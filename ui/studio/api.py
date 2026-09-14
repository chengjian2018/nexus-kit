"""Studio API —— /api/v1/studio/*（编排工作台的后端面）。

端点分四组（详见 README·studio 节）：

1. pattern 管理面：列表（source=code|console 徽标）/ 详情 / 校验 / 发布 /
   fork / 删除。发布与 CLI ``pattern-load`` 同一条三段式通路
   （from_yaml → validate → register），落盘到 store.py 的托管目录。
2. agent 生成：``POST /generate`` SSE 流（status/step/delta/result 事件）——
   驱动本地 Claude Code（``claude -p --output-format stream-json --verbose
   --allowedTools Read,Grep,Glob,LS``，stdin 喂提示词，cwd 固定项目根仅供
   只读探索），工具调用以 step 事件、文本增量以 delta 事件实时回传；最终
   答复解析出 pattern YAML + 插件模块，插件先经临时文件导入验证（注册但
   未持久化），用户预览后 ``POST /apply`` 一键落盘。应用顺序固定「先插件
   后 pattern」（pattern 校验要解析插件 code）。
3. AI 助手：``POST /assist`` 非流式（节点话术 / 回答范式 / 执行器生成）。
4. 目录：catalog（流程编排下拉数据源）。

约定与 ui/api.py 一致：响应 {code, message, status, data} 包裹；挂 /api/v1/
下天然被 NEXUS_API_KEY 中间件覆盖；除 generate/assist（子进程/LLM I/O）外
全部同步 def。assist 取服务配置 llm_default（nexus.settings.get_llm_config），
generate 用本机 claude CLI（可用环境变量 NEXUS_STUDIO_CLAUDE_BIN 覆盖二进制
路径），页面不提供模型/提供商选择。插件落盘前一律先临时导入验证——托管
目录里永远不会出现未经验证的代码；临时导入的注册副作用是进程级的，未
apply 的插件重启后自然消失。
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

# Claude Code（自动编排生成引擎）：二进制可经环境变量覆盖（测试/开发用）
_CLAUDE_BIN = environ.get("NEXUS_STUDIO_CLAUDE_BIN", "claude")

# 只读探索白名单：产物契约是「围栏文本经 stdout 返回」，一切落盘统一走
# /apply 的托管目录（store.py）。子进程因此不授任何写权限——未列入白名单
# 的工具（Write/Edit/Bash/WebFetch…）在 -p 非交互模式下自动拒绝，生成产物
# 不可能经子进程散落进仓库。刻意不用 --dangerously-skip-permissions：它会
# 绕过包括 deny 规则在内的一切权限检查，无法收敛写入面。
_CLAUDE_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "LS")

# claude 子进程的工作目录：本仓库根（ui/studio/api.py 上溯两级）——
# 「自动编排」的读写面即当前项目
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# step 事件 detail 的截断长度（工具入参/结果只做过程展示，不搬全量）
_STEP_DETAIL_MAX = 240


def _ensure_discovery() -> None:
    """首次请求前暖注册表（host.main import 时已做过；独立挂载/测试懒触发）。"""
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
# Pattern 管理面
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
    # 已落盘但装载失败（注册表里没有）的 console pattern 也要露出错误态
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
    """dry-run：构造 + 收集式校验，不注册不落盘。

    工具面宽松（见 agent.validate_pattern_text）：未注册/越集工具进
    warnings 不进 errors。"""
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
    """发布：三段式（from_yaml → validate → register）+ 落盘托管目录。

    新会话即生效（运行中会话持旧引用跑完当前轮，与 /reload 语义一致）。
    工具面宽松：引用未注册的新工具不阻塞发布（warnings 透出）。
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
    """把代码内置 pattern 导出为 console 托管副本（fork-to-edit）。"""
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
    """删除 console 托管 pattern（仅 console；注销注册 + 删文件）。

    若该 code 存在代码内置版本，重启/重载后内置版自动恢复。
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
# 目录与模型选项
# ---------------------------------------------------------------------------

@router.get("/catalog")
def catalog() -> Dict[str, Any]:
    """流程编排下拉数据源：已注册插件 codes（按 kind）+ 工具集。"""
    _ensure_discovery()
    return _ok({
        "executor": plugin_registry.list_codes("executor"),
        "stage": plugin_registry.list_codes("stage"),
        "messages_builder": plugin_registry.list_codes("messages_builder"),
        "toolsets": tool_registry.get_available_toolsets(),
    })


# ---------------------------------------------------------------------------
# SSE 工具 / Claude Code（本地 CLI）驱动
# ---------------------------------------------------------------------------

def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _tool_detail(name: str, tool_input: Dict[str, Any]) -> str:
    """挑工具入参里最能代表「正在做什么」的一个字段做单行摘要。"""
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:_STEP_DETAIL_MAX]
    return json.dumps(tool_input, ensure_ascii=False)[:_STEP_DETAIL_MAX]


def _tool_result_text(content: Any) -> str:
    """tool_result 的 content 可能是 str / 文本块列表 / dict，拍平成纯文本。"""
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
    """驱动本地 Claude Code（print 模式）并把执行过程翻译成 studio 事件。

    子进程：``claude -p --output-format stream-json --verbose
    --include-partial-messages --allowedTools Read,Grep,Glob,LS``（cwd =
    项目根，只读探索；未白名单的写类工具在非交互 -p 模式下自动拒绝），
    提示词经 stdin 喂入。stream-json 每行一个 JSON 事件，映射关系：

    - system/init           → status（模型 + session）
    - stream_event 文本增量 → delta（--include-partial-messages）
    - assistant/tool_use    → step{phase: run}（工具名 + 入参摘要）
    - user/tool_result      → step{phase: result}（结果摘要，出错标 err）
    - result                → status（轮数/耗时）+ final{text}（最终答复，
                              供上层做围栏解析；空时回退累计增量文本）

    任何失败（CLI 不存在 / 非零退出 / result.subtype 非 success）yield 一个
    error 事件后结束。客户端断开（generator 被 close）时 kill 子进程。
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
    proc.stdin.close()  # EOF：claude 把整段 stdin 当作本次任务提示词

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

    assistant_text: List[str] = []  # 完整 assistant 文本块（final 兜底用）
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
    """围栏之前的说明文字。"""
    match = re.search(r"```", raw)
    return (raw[:match.start()] if match else raw).strip()


def _preview_plugins(parsed_plugins: List[Dict[str, Any]],
                     ) -> List[Dict[str, Any]]:
    """逐个临时导入验证生成插件（注册但未持久化）。

    模块 exec 即注册（替换窗口内），后续 pattern 校验因此能解析这些 code；
    未 apply 的注册是进程级的，重启后自然消失。
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
# 自动编排：生成（SSE）与应用
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
    """自动编排生成（SSE）：status → (status|step|delta)* → result | error。

    生成引擎是本地 Claude Code：build_generate_messages 产出的提示词
    （schema + 插件目录 + 范例 + 用户需求）喂给 run_claude_code（claude
    -p，完全权限，项目根执行），执行步骤与文本增量实时回传；最终答复
    走与旧 LLM 通路相同的围栏解析 → 插件预览导入 → pattern 校验。
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
            # 插件先预览导入（exec 即注册）——pattern 若引用了生成插件的
            # code，校验必须在注册之后跑，否则误报「未注册」
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
    """(stem, error)：临时导入验证一个待应用插件；error 空 = 通过。

    验证通过前不写托管目录——目录里永远不会出现未经验证的代码。
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
    """应用生成结果：插件先验证再落盘+装载，pattern 三段式+落盘。

    顺序固定先插件后 pattern（pattern 校验要解析插件 code）；任何插件
    验证失败都在落盘前中止（400），不会留下半套产物。
    """
    _ensure_discovery()
    # pass 1：全部插件先临时验证（注册副作用进程级，未落盘）
    verified: List[Tuple[str, str]] = []
    for pl in body.plugins:
        stem, error = _verify_plugin_text(pl.filename, pl.code)
        if error:
            return _fail(400, "400", f"插件 {pl.filename} 验证失败: {error}")
        verified.append((stem, pl.code))
    # pass 2：落盘 + 从托管路径重新装载（fresh exec + 替换窗口重注册）
    applied: List[str] = []
    try:
        for stem, code_text in verified:
            path = store.write_plugin_file(stem, code_text)
            store.import_plugin_module(path)
            applied.append(f"{stem}.py")
    except Exception as e:
        return _fail(500, "500", f"插件落盘失败: {e}")
    # pattern：校验 → 落盘 → 注册（工具面宽松：未注册的新工具不阻塞应用）
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
# 流程编排 AI 助手（非流式）
# ---------------------------------------------------------------------------

class AssistIn(BaseModel):
    mode: str = Field(min_length=1, max_length=32)
    payload: Dict[str, Any] = Field(default_factory=dict)


@router.post("/assist")
async def assist(body: AssistIn):
    """非流式小助手：node_prompt / node_examples / plugin_generate。"""
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
