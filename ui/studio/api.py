"""Studio API — /api/v1/studio/* (backend surface of the orchestration workbench).

Endpoints fall into four groups (see the README studio section for details):

1. Pattern management: list (with source=code|console badge) / detail /
   validate / publish / fork / delete. Publishing goes through the same
   three-stage pipeline as the CLI ``pattern-load``
   (from_yaml → validate → register), persisting into store.py's hosted
   directories.
2. Agent generation: the ``POST /generate`` SSE stream (status/step/delta/
   result events) — drives the local Claude Code (``claude -p
   --output-format stream-json --verbose --allowedTools Read,Grep,Glob,LS``;
   the prompt is fed via stdin and cwd is pinned to the project root for
   read-only exploration only); tool calls stream back live as step events
   and text increments as delta events; the final reply is parsed into a
   pattern YAML + plugin modules, with plugins first verified via a
   temporary-file import (registered but not persisted); after the user
   previews, ``POST /apply`` persists them in one go. The apply order is
   fixed "plugins first, then patterns" (pattern validation needs to resolve
   plugin codes).
3. AI assistant: ``POST /assist``, non-streaming (node scripts / answer
   examples / executor generation).
4. Catalog: catalog (data source for the flow-orchestration dropdown).

Conventions match ui/api.py: responses use the {code, message, status, data}
envelope; routes mounted under /api/v1/ are naturally covered by the
NEXUS_API_KEY middleware; everything is sync def except generate/assist
(subprocess/LLM I/O). assist takes the service config llm_default
(nexus.settings.get_llm_config); generate uses the local claude CLI (the
binary path can be overridden via the NEXUS_STUDIO_CLAUDE_BIN environment
variable); the page offers no model/provider selection. Plugins are always
verified via a temporary import before being persisted — unverified code
never appears in the hosted directories; the registration side effects of a
temporary import are process-level, so plugins that are never applied simply
vanish on restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from collections import deque
from os import environ
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from nexus.llm.resolve import build_provider
from nexus.model.serialization import pattern_to_dict, pattern_to_yaml
from nexus.registry.patterns import (
    discover_builtin_patterns,
    registry as pattern_registry,
)
from nexus.registry.plugins import (
    discover_builtin_plugins,
    registry as plugin_registry,
)
from nexus.registry.tools import discover_builtin_tools, registry as tool_registry
from nexus.visualize import pattern_to_mermaid
from nexus.settings import get_llm_config

from ui.studio import agent, store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/studio")

_discovered = False

# Claude Code (the auto-orchestration generation engine): the binary is overridable via env var (tests/dev)
_CLAUDE_BIN = environ.get("NEXUS_STUDIO_CLAUDE_BIN", "claude")

# Read-only exploration whitelist: the artifact contract is "fenced text
# returned via stdout"; every write lands through /apply's hosted directory
# (store.py). The subprocess therefore gets NO write permissions — tools
# outside the whitelist (Write/Edit/Bash/WebFetch…) are auto-rejected in -p
# non-interactive mode, so generated artifacts cannot scatter into the repo
# via the subprocess. --dangerously-skip-permissions is deliberately not
# used: it bypasses every permission check including deny rules and cannot
# converge the write surface.
_CLAUDE_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "LS")

# The claude subprocess's working directory: this repo root (two levels up
# from ui/studio/api.py) — the auto-orchestration read/write face IS the
# current project
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# step-event detail truncation length (tool args/results are process display only, never the full text)
_STEP_DETAIL_MAX = 240


def _ensure_discovery() -> None:
    """Warm the registries before the first request (host.main's import already did it; lazy in standalone mounts/tests)."""
    global _discovered
    if _discovered:
        return
    discover_builtin_tools()
    discover_builtin_patterns()
    discover_builtin_plugins()
    _discovered = True


def _ok(data: Any = None, message: str = "success") -> Dict[str, Any]:
    return {"code": "0", "status": True, "message": message,
            "data": data if data is not None else {}}


def _fail(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "status": False, "message": message},
    )


# ---------------------------------------------------------------------------
# Pattern management
# ---------------------------------------------------------------------------

def _pattern_meta(pattern: Any, source: str = "code") -> Dict[str, Any]:
    return {
        "code": pattern.code,
        "name": pattern.name,
        "description": pattern.description,
        "pattern_type": getattr(pattern, "pattern_type", "agent"),
        "entry_node_code": pattern.entry_node_code,
        "node_count": len(pattern.nodes or []),
        "plugins": dict(pattern.plugins or {}),
        "allow_toolset": list(pattern.allow_toolset or []),
        "max_steps": getattr(pattern, "max_steps", None),
        "source": source,
    }


def _pattern_source(code: str) -> str:
    return "console" if code in store.console_pattern_codes() else "code"


@router.get("/patterns")
def list_patterns() -> Dict[str, Any]:
    _ensure_discovery()
    console_codes = store.console_pattern_codes()
    patterns = [
        _pattern_meta(p, source="console" if p.code in console_codes else "code")
        for p in pattern_registry.list_patterns()
    ]
    # Console patterns persisted but failed-to-load (absent from the registry) must also surface the error state
    registered = {p["code"] for p in patterns}
    for code in sorted(console_codes - registered):
        patterns.append({
            "code": code, "name": "", "description": "",
            "pattern_type": "", "entry_node_code": "", "node_count": 0,
            "plugins": {}, "allow_toolset": [], "max_steps": None,
            "source": "console", "load_error": store.pattern_load_error(code),
        })
    return _ok({"patterns": patterns})


@router.get("/patterns/{code}")
def get_pattern_detail(code: str):
    _ensure_discovery()
    pattern = pattern_registry.get(code)
    if pattern is None:
        return _fail(404, "404",
                     f"pattern '{code}' 未注册，已注册: "
                     f"{pattern_registry.list_codes()}")
    try:
        return _ok({
            "meta": _pattern_meta(pattern, source=_pattern_source(code)),
            "yaml": pattern_to_yaml(pattern),
            "mermaid": pattern_to_mermaid(pattern),
            "tree": pattern_to_dict(pattern),
        })
    except Exception:
        logger.exception("pattern 序列化失败: %s", code)
        return _fail(500, "500", f"pattern '{code}' 序列化失败，详情见服务日志")


class YamlIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=400_000)


@router.post("/patterns/validate")
def validate_pattern_yaml(body: YamlIn):
    """Dry-run: construction + collected validation; no registration, no
    persistence.

    The tool surface is lenient (see agent.validate_pattern_text):
    unregistered/out-of-set tools go into warnings, not errors."""
    _ensure_discovery()
    pattern, errors, warnings = agent.validate_pattern_text(body.yaml)
    data: Dict[str, Any] = {"errors": errors, "warnings": warnings,
                            "constructed": pattern is not None}
    if pattern is not None:
        data["meta"] = _pattern_meta(pattern, source=_pattern_source(pattern.code))
        try:
            data["mermaid"] = pattern_to_mermaid(pattern)
        except Exception:
            data["mermaid"] = ""
    return _ok(data)


@router.post("/patterns/publish")
def publish_pattern(body: YamlIn):
    """Publish: the three stages (from_yaml → validate → register) + persist to the hosted directory.

    Effective for new sessions immediately (running sessions keep old
    references through their current turn — the same semantics as /reload).
    Lenient tool surface: referencing an unregistered new tool does not
    block publishing (the warnings are surfaced).
    """
    _ensure_discovery()
    pattern, errors, warnings = agent.validate_pattern_text(body.yaml)
    if pattern is None or errors:
        return _fail(400, "400",
                     "pattern 校验失败，未发布:\n" + "\n".join(errors))
    try:
        store.write_pattern_file(pattern.code, body.yaml)
        store.load_pattern_text(body.yaml)
    except ValueError as e:
        return _fail(400, "400", str(e))
    except OSError as e:
        return _fail(500, "500", f"pattern 落盘失败: {e}")
    return _ok({
        "meta": _pattern_meta(pattern, source="console"),
        "mermaid": pattern_to_mermaid(pattern),
        "warnings": warnings,
    }, message=f"已发布并生效: {pattern.code}（新会话起用新版）")


class ForkIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


@router.post("/patterns/fork")
def fork_pattern(body: ForkIn):
    """Export a code-builtin pattern into a console-hosted copy (fork-to-edit)."""
    _ensure_discovery()
    pattern = pattern_registry.get(body.code)
    if pattern is None:
        return _fail(404, "404", f"pattern '{body.code}' 未注册")
    try:
        yaml_text = pattern_to_yaml(pattern)
        store.write_pattern_file(pattern.code, yaml_text)
    except Exception as e:
        return _fail(500, "500", f"fork 失败: {e}")
    return _ok({
        "meta": _pattern_meta(pattern, source="console"),
        "yaml": yaml_text,
    }, message=f"已 fork 为可编辑副本: {pattern.code}（后续以控制台版本为准）")


@router.delete("/patterns/{code}")
def delete_pattern(code: str):
    """Delete a console-hosted pattern (console-only; deregister + delete the file).

    If a code-builtin version of the code exists, it comes back automatically
    after a restart/reload.
    """
    _ensure_discovery()
    if code not in store.console_pattern_codes():
        return _fail(404, "404",
                     f"pattern '{code}' 不是 console 托管版本，不可删除")
    try:
        store.delete_pattern_file(code)
    except ValueError as e:
        return _fail(400, "400", str(e))
    pattern_registry.deregister(code)
    return _ok({"code": code},
               message=f"已删除 {code}（若存在代码内置版本，重启/重载后恢复）")


# ---------------------------------------------------------------------------
# Catalog and model options
# ---------------------------------------------------------------------------

@router.get("/catalog")
def catalog() -> Dict[str, Any]:
    """The flow-editing dropdown data source: registered plugin codes (by kind) + toolsets."""
    _ensure_discovery()
    return _ok({
        "executor": plugin_registry.list_codes("executor"),
        "stage": plugin_registry.list_codes("stage"),
        "messages_builder": plugin_registry.list_codes("messages_builder"),
        "toolsets": tool_registry.get_available_toolsets(),
    })


# ---------------------------------------------------------------------------
# SSE helpers / Claude Code (local CLI) driving
# ---------------------------------------------------------------------------

def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _tool_detail(name: str, tool_input: Dict[str, Any]) -> str:
    """Pick the tool-input field that best represents "what is happening" for a one-line summary."""
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:_STEP_DETAIL_MAX]
    return json.dumps(tool_input, ensure_ascii=False)[:_STEP_DETAIL_MAX]


def _tool_result_text(content: Any) -> str:
    """A tool_result's content may be a str / a list of text blocks / a dict — flatten it to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p)
    if isinstance(content, dict):
        return content.get("text", "") or json.dumps(content, ensure_ascii=False)
    return ""


async def run_claude_code(prompt: str) -> AsyncGenerator[Dict[str, Any], None]:
    """Drive the local Claude Code (print mode) and translate the execution process into studio events.

    Subprocess: ``claude -p --output-format stream-json --verbose
    --include-partial-messages --allowedTools Read,Grep,Glob,LS`` (cwd =
    project root, read-only exploration; non-whitelisted write tools are
    auto-rejected in non-interactive -p mode); the prompt is fed via stdin.
    stream-json emits one JSON event per line, mapped as:

    - system/init           → status (model + session)
    - stream_event text delta → delta (--include-partial-messages)
    - assistant/tool_use    → step{phase: run} (tool name + argument summary)
    - user/tool_result      → step{phase: result} (result summary, flagged
                              err on failure)
    - result                → status (turn count / elapsed time) +
                              final{text} (the final reply, for the caller's
                              fence parsing; falls back to the accumulated
                              delta text when empty)

    Any failure (CLI missing / nonzero exit / result.subtype not success)
    yields one error event and ends. When the client disconnects (the
    generator is closed), the subprocess is killed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _CLAUDE_BIN, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--allowedTools", *_CLAUDE_ALLOWED_TOOLS,
            cwd=str(_PROJECT_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environ, NO_COLOR="1"),
        )
    except OSError as e:
        yield {"kind": "error",
               "message": f"本地 Claude Code CLI 不可用（{_CLAUDE_BIN} 不在 "
                          f"PATH 或不可执行）: {e}"}
        return

    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()  # EOF: claude treats the entire stdin as this run's task prompt

    stderr_tail: deque = deque(maxlen=30)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                stderr_tail.append(text)

    drain_task = asyncio.create_task(_drain_stderr())
    yield {"kind": "status", "stage": "claude",
           "message": f"Claude Code 执行中（只读探索 · {_PROJECT_ROOT.name}/）…"}

    assistant_text: List[str] = []  # full assistant text blocks (fallback source for final)
    final_text = ""
    result_meta: Dict[str, Any] = {}
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                logger.debug("claude stream-json 行解析失败: %.120s", line)
                continue
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                session = str(event.get("session_id") or "")[:8]
                yield {"kind": "status", "stage": "claude",
                       "message": f"已启动 {event.get('model') or 'claude'}"
                                  f"（session {session}…）"}
            elif etype == "stream_event":
                sev = event.get("event") or {}
                delta = sev.get("delta") or {}
                if (sev.get("type") == "content_block_delta"
                        and delta.get("type") == "text_delta"
                        and delta.get("text")):
                    yield {"kind": "delta", "text": delta["text"]}
            elif etype == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        assistant_text.append(block["text"])
                    elif block.get("type") == "tool_use":
                        yield {"kind": "step", "phase": "run",
                               "id": block.get("id") or "",
                               "name": block.get("name") or "tool",
                               "detail": _tool_detail(
                                   block.get("name") or "tool",
                                   block.get("input") or {})}
            elif etype == "user":
                for block in (event.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        yield {"kind": "step", "phase": "result",
                               "id": block.get("tool_use_id") or "",
                               "name": "result",
                               "detail": " ".join(_tool_result_text(
                                   block.get("content")).split()
                               )[:_STEP_DETAIL_MAX],
                               "is_error": bool(block.get("is_error"))}
            elif etype == "result":
                final_text = event.get("result") or ""
                result_meta = event
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(drain_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            drain_task.cancel()

    if proc.returncode != 0:
        tail = " | ".join(list(stderr_tail)[-5:])
        yield {"kind": "error",
               "message": f"Claude Code 退出码 {proc.returncode}: "
                          f"{tail or '无 stderr 输出'}"}
        return
    if result_meta and result_meta.get("subtype") != "success":
        yield {"kind": "error",
               "message": f"Claude Code 未成功结束"
                          f"（{result_meta.get('subtype')}）"}
        return
    if not final_text.strip():
        final_text = "".join(assistant_text)
    secs = round((result_meta.get("duration_ms") or 0) / 1000, 1)
    yield {"kind": "status", "stage": "claude",
           "message": f"Claude Code 执行完成"
                      f"（{result_meta.get('num_turns', '?')} 轮 · {secs}s）"}
    yield {"kind": "final", "text": final_text,
           "num_turns": result_meta.get("num_turns"),
           "duration_ms": result_meta.get("duration_ms")}


def _explanation_of(raw: str) -> str:
    """The prose before the fence."""
    match = re.search(r"```", raw)
    return (raw[:match.start()] if match else raw).strip()


def _preview_plugins(parsed_plugins: List[Dict[str, Any]],
                     ) -> List[Dict[str, Any]]:
    """Import-verify the generated plugins one by one (registered but not persisted).

    A module's exec registers it (inside the replacement window), so the
    later pattern validation can resolve those codes; an un-applied
    registration is process-level and disappears naturally on restart.
    """
    results: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="studio_plugin_") as td:
        for pl in parsed_plugins:
            entry: Dict[str, Any] = {
                "filename": pl.get("filename"),
                "stem": pl.get("stem"),
                "declarations": [
                    {"kind": k, "code": c} for k, c in pl.get("declarations", [])],
                "imported": False, "error": "",
            }
            if not pl.get("stem"):
                entry["error"] = ("无法解析插件文件名（缺 studio-plugin 首行注释"
                                  "或合法注册声明）")
                results.append(entry)
                continue
            tmp = Path(td) / f"{pl['stem']}.py"
            tmp.write_text(pl["code_text"], encoding="utf-8")
            try:
                store.import_plugin_module(tmp, stem=pl["stem"])
            except Exception as e:
                logger.exception("生成插件导入失败: %s", pl["stem"])
                entry["error"] = f"导入失败: {e}"
                results.append(entry)
                continue
            if not pl.get("declarations"):
                entry["error"] = "模块没有可识别的 plugin_registry.register 声明"
                results.append(entry)
                continue
            missing = agent.declared_codes_registered(pl["declarations"])
            if missing:
                entry["error"] = f"注册声明未生效: {', '.join(missing)}"
            else:
                entry["imported"] = True
            results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Auto-orchestration: generate (SSE) and apply
# ---------------------------------------------------------------------------

class GenerateIn(BaseModel):
    background: str = Field(min_length=1, max_length=6000)
    features: str = Field(min_length=1, max_length=10000)
    examples: str = Field(default="", max_length=10000)
    extra: str = Field(default="", max_length=6000)
    name: str = Field(default="", max_length=128)
    code: str = Field(default="", max_length=64)


@router.post("/generate")
async def generate(body: GenerateIn):
    """Auto-orchestration generation (SSE): status → (status|step|delta)* → result | error.

    The generation engine is the local Claude Code: the prompt produced by
    build_generate_messages (schema + plugin catalog + exemplar + the
    user's requirement) feeds run_claude_code (claude -p, the read-only
    exploration whitelist, running at the project root); execution steps
    and text deltas stream back in real time; the final answer goes through
    the same fence parsing → plugin preview-import → pattern validation as
    the earlier LLM path.
    """
    _ensure_discovery()

    async def _gen() -> AsyncGenerator[str, None]:
        try:
            yield _sse({"kind": "status", "stage": "prompt",
                        "message": "组装提示词（schema + 插件目录 + 范例）…"})
            prompt = "\n\n".join(
                m["content"]
                for m in agent.build_generate_messages(body.model_dump()))

            raw = ""
            saw_error = False
            async for ev in run_claude_code(prompt):
                if ev["kind"] == "final":
                    raw = ev["text"]
                    continue
                if ev["kind"] == "error":
                    saw_error = True
                yield _sse(ev)
            if saw_error:
                return
            if not raw.strip():
                yield _sse({"kind": "error",
                            "message": "Claude Code 未返回任何文本（可重试）"})
                return

            yield _sse({"kind": "status", "stage": "parse",
                        "message": "解析输出并校验…"})
            parsed = agent.parse_generation_output(raw)
            result: Dict[str, Any] = {
                "raw": raw,
                "explanation": _explanation_of(raw),
                "yaml": parsed["yaml"],
                "plugins": [],
                "pattern_errors": [],
                "pattern_warnings": [],
                "ok": False,
            }
            # Preview-import plugins first (exec registers) — if the pattern
            # references a generated plugin's code, validation must run after
            # registration or it would falsely report "unregistered"
            result["plugins"] = _preview_plugins(parsed["plugins"])
            if parsed["yaml"] is None:
                result["pattern_errors"] = [
                    "输出中没有可识别的 ```yaml pattern 围栏（可重试，或在原始"
                    "输出中检查模型行为）"]
            else:
                pattern, errors, warnings = agent.validate_pattern_text(
                    parsed["yaml"])
                result["pattern_errors"] = errors
                result["pattern_warnings"] = warnings
                if pattern is not None:
                    result["meta"] = _pattern_meta(pattern, source="console")
                    try:
                        result["mermaid"] = pattern_to_mermaid(pattern)
                    except Exception:
                        result["mermaid"] = ""
            plugin_errors = [p["error"] for p in result["plugins"] if p["error"]]
            result["ok"] = (
                parsed["yaml"] is not None and not result["pattern_errors"]
                and not plugin_errors)
            yield _sse({"kind": "result", "result": result})
        except Exception as e:
            logger.exception("studio generate 失败")
            yield _sse({"kind": "error",
                        "message": f"生成失败: {e}（详情见服务日志）"})

    return StreamingResponse(_gen(), media_type="text/event-stream")


class ApplyPluginIn(BaseModel):
    filename: str = Field(min_length=1, max_length=80)
    code: str = Field(min_length=1, max_length=200_000)


class ApplyIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=400_000)
    plugins: List[ApplyPluginIn] = Field(default_factory=list)


def _verify_plugin_text(filename: str, code_text: str) -> Tuple[str, str]:
    """(stem, error): verify a plugin pending apply via a temporary import;
    empty error = pass.

    The hosted directories are never written before verification passes —
    unverified code never appears there.
    """
    stem = re.sub(r"\.py$", "", filename).lower()
    try:
        store.check_stem(stem)
    except ValueError as e:
        return stem, str(e)
    parsed = agent.parse_plugin_block(code_text)
    if not parsed["declarations"]:
        return stem, "插件代码没有可识别的 plugin_registry.register 声明"
    with tempfile.TemporaryDirectory(prefix="studio_plugin_") as td:
        tmp = Path(td) / f"{stem}.py"
        tmp.write_text(code_text, encoding="utf-8")
        try:
            store.import_plugin_module(tmp, stem=stem)
        except Exception as e:
            return stem, f"导入失败: {e}"
    missing = agent.declared_codes_registered(parsed["declarations"])
    if missing:
        return stem, f"注册声明未生效: {', '.join(missing)}"
    return stem, ""


@router.post("/apply")
def apply_artifacts(body: ApplyIn):
    """Apply the generated artifacts: plugins verified first, then persisted + loaded; pattern three-stage + persisted.

    The order is fixed — plugins before the pattern (pattern validation must
    resolve plugin codes); any plugin verification failure aborts before
    persisting (400), never leaving half a set of artifacts behind.
    """
    _ensure_discovery()
    # pass 1: temporarily verify all plugins first (the registration side effect is process-level, nothing persisted)
    verified: List[Tuple[str, str]] = []
    for pl in body.plugins:
        stem, error = _verify_plugin_text(pl.filename, pl.code)
        if error:
            return _fail(400, "400", f"插件 {pl.filename} 验证失败: {error}")
        verified.append((stem, pl.code))
    # pass 2: persist + reload from the hosted path (fresh exec + re-register inside the replacement window)
    applied: List[str] = []
    try:
        for stem, code_text in verified:
            path = store.write_plugin_file(stem, code_text)
            store.import_plugin_module(path)
            applied.append(f"{stem}.py")
    except Exception as e:
        return _fail(500, "500", f"插件落盘失败: {e}")
    # pattern: validate → persist → register (lenient tool surface: unregistered new tools do not block apply)
    pattern, errors, warnings = agent.validate_pattern_text(body.yaml)
    if pattern is None or errors:
        return _fail(400, "400",
                     "pattern 校验失败，未应用:\n" + "\n".join(errors))
    try:
        store.write_pattern_file(pattern.code, body.yaml)
        store.load_pattern_text(body.yaml)
    except ValueError as e:
        return _fail(400, "400", str(e))
    except OSError as e:
        return _fail(500, "500", f"pattern 落盘失败: {e}")
    return _ok({
        "meta": _pattern_meta(pattern, source="console"),
        "mermaid": pattern_to_mermaid(pattern),
        "plugins": applied,
        "warnings": warnings,
    }, message=f"已应用并生效: {pattern.code}"
               f"（插件 {len(applied)} 个）——可去模版测试试用")


# ---------------------------------------------------------------------------
# Flow-editing AI assistant (non-streaming)
# ---------------------------------------------------------------------------

class AssistIn(BaseModel):
    mode: str = Field(min_length=1, max_length=32)
    payload: Dict[str, Any] = Field(default_factory=dict)


@router.post("/assist")
async def assist(body: AssistIn):
    """Non-streaming mini assistant: node_prompt / node_examples / plugin_generate."""
    _ensure_discovery()
    try:
        messages = agent.build_assist_messages(body.mode, body.payload)
    except ValueError as e:
        return _fail(400, "400", str(e))
    try:
        cfg = get_llm_config()
        provider = build_provider(cfg)
    except Exception as e:
        return _fail(400, "400", f"LLM 配置/构建失败（llm_default）: {e}")
    max_tokens = max(2048, int(cfg.get("max_tokens", 2048)))
    try:
        result = await provider.achat_completion(
            messages=messages, model=cfg.get("model"),
            temperature=cfg.get("temperature", 0.7), max_tokens=max_tokens)
    except Exception as e:
        logger.exception("studio assist 失败: mode=%s", body.mode)
        return _fail(500, "500", f"LLM 调用失败: {e}")
    text = result.get("content", "")
    data: Dict[str, Any] = {"text": text}
    if body.mode == "plugin_generate":
        parsed = agent.parse_generation_output(text)
        plugin = parsed["plugins"][0] if parsed["plugins"] else None
        if plugin:
            data["plugin"] = {
                "filename": plugin.get("filename"),
                "stem": plugin.get("stem"),
                "declarations": [
                    {"kind": k, "code": c}
                    for k, c in plugin.get("declarations", [])],
                "code_text": plugin["code_text"],
            }
    else:
        data["json"] = agent.parse_assist_json(text)
    return _ok(data)
