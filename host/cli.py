"""hermes-nexus debug CLI — exercise template dialogues from the command line.

Subcommands (fire dispatch):
    cli.py chat                  interactive REPL (default)
    cli.py ask "Hello"           one-shot Q&A (--session-id resumes a session from the db)
    cli.py list                  list registered patterns / llm providers / tools
    cli.py sessions              list sessions in data/dialogue.db

Selection UX: starting without --pattern/--llm pops the prompt_toolkit arrow-key
menu (pattern single-level; llm two-level provider → models). After picking a
pattern you can enter task_info JSON (Enter skips; --task-info passes it
directly; patterns with a mock preset such as customer_agent adopt the mock on
Enter, see MOCK_TASK_INFO). Debug output: -v summary / -vv full.

Usage examples:
    .venv/bin/python cli.py chat --pattern xianyu_agent -vv
    .venv/bin/python cli.py ask "Does this ship free?" --session-id t1
    .venv/bin/python cli.py list patterns
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 仓库是扁平顶层包布局(nexus/ atoms/ apps/ host/ 同级,pytest 靠根
# conftest 注入 sys.path)。直接以 `python cli.py` 启动时脚本所在目录
# (host/)成为 sys.path[0],仓库根不在路径上——这里自举补上,使 CLI 从
# 任何 CWD、任何解释器启动都可用(与 python -m host.cli 等效)
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import fire  # noqa: E402

# 触发 host.config 的 set_config_path 副作用:裸 `python cli.py` 启动时
# host 不作为包被 import,配置路径会退化成 CWD 探测而从非仓库根启动时
# 找不到 local_config.yaml(MCP bootstrap / llm 解析都依赖它)
import host.config  # noqa: E402,F401

from nexus.engine.chat import chat as chat_turn  # noqa: E402
from nexus.engine.session import Session
from nexus.engine.store import SessionStore
from nexus.registry.patterns import discover_builtin_patterns, registry as pattern_registry
from nexus.registry.plugins import discover_builtin_plugins
from nexus.registry.providers import discover_builtin_providers, registry as llm_registry
from nexus.registry.tools import discover_builtin_tools
from nexus.registry.tools import registry as tool_registry
from nexus.settings import get_llm_config, get_session_db_path

# ============================================================================
# ANSI coloring (auto-falls back on NO_COLOR / non-TTY; see https://no-color.org)
# ============================================================================

_COLOR_OK = sys.stdout.isatty() and "NO_COLOR" not in os.environ

def _c(text: str, code: str) -> str:
    if not _COLOR_OK:
        return text
    return f"\033[{code}m{text}\033[0m"

def dim(t):    return _c(t, "2")
def bold(t):   return _c(t, "1")
def green(t):  return _c(t, "32")
def cyan(t):   return _c(t, "36")
def yellow(t): return _c(t, "33")
def red(t):    return _c(t, "31")


# ============================================================================
# Pure functions: menu rendering / verbose formatting / slash command parsing (unit-tested)
# ============================================================================

def render_pattern_menu(title: str, patterns: List[Any]) -> str:
    """Testable rendering of the numbered menu (actual selection uses the prompt_toolkit radiolist)."""
    lines = [bold(title)]
    for i, p in enumerate(patterns, 1):
        lines.append(f"  {i}. {p.code} — {p.name}")
        if getattr(p, "description", ""):
            lines.append(dim(f"     {p.description}"))
    return "\n".join(lines)


def render_verbose_summary(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """-v level: node transitions, intent/next_node, slot changes.

    before/after are per-turn snapshot dicts: current_node_code / current_module_code /
    filled_slots / intent / next_node (missing keys are ignored as unchanged).
    """
    lines: List[str] = []

    node_from, node_to = before.get("current_node_code"), after.get("current_node_code")
    mod_from, mod_to = before.get("current_module_code"), after.get("current_module_code")
    if mod_from != mod_to and node_from != node_to:
        lines.append(dim(f"  [{mod_from}·{node_from}] → [{mod_to}·{node_to}]"))
    elif node_from != node_to:
        lines.append(dim(f"  node: {node_from} → {node_to}"))
    elif mod_from != mod_to:
        lines.append(dim(f"  module: {mod_from} → {mod_to}"))

    intent = after.get("intent")
    if intent:
        lines.append(dim(f"  intent: {intent}"))
    next_node = after.get("next_node")
    if next_node and next_node != node_from:
        lines.append(dim(f"  next_node: {next_node}"))

    slots_before = before.get("filled_slots") or {}
    slots_after = after.get("filled_slots") or {}
    changed = {k: v for k, v in slots_after.items() if slots_before.get(k) != v}
    if changed:
        parts = [f"{k}={v!r}" for k, v in sorted(changed.items())]
        lines.append(dim(f"  slots: {', '.join(parts)}"))

    return "\n".join(lines)


def render_verbose_full(cxt) -> str:
    """-vv level: full nlu/nlg JSON, recall, actions, agent tool calls."""
    lines: List[str] = [dim("  ── context ──")]

    if getattr(cxt, "nlu_result", None):
        lines.append(dim("  nlu_result: " + json.dumps(cxt.nlu_result, ensure_ascii=False)))
    if getattr(cxt, "nlg_result", None):
        lines.append(dim("  nlg_result: " + json.dumps(cxt.nlg_result, ensure_ascii=False)))
    if getattr(cxt, "agent_result", None):
        lines.append(dim("  agent_result: " + _safe_json(cxt.agent_result)))

    recall = getattr(cxt, "format_recall_info", None)
    if recall and (recall_info := recall()):
        lines.append(dim("  recall: " + recall_info.replace("\n", " | ")))

    actions = getattr(cxt, "actions", None) or []
    if actions:
        from nexus.context import ModuleJumpEvent

        rendered = [
            item.to_dict() if isinstance(item, ModuleJumpEvent) else item
            for item in actions
        ]
        lines.append(dim("  actions: " + _safe_json(rendered)))

    return "\n".join(lines)


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(obj)


def parse_slash_command(line: str) -> Optional[Dict[str, Any]]:
    """Parse REPL slash commands; return None for non-slash input.

    Returns dict(name=..., arg=...); arg is the remaining text after the
    command (may be empty). Unknown commands return
    dict(name="unknown", arg=original command word).
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    name = parts[0][1:].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if name not in _SLASH_COMMANDS:
        return {"name": "unknown", "arg": name}
    return {"name": name, "arg": arg}


_SLASH_COMMANDS = ("help", "exit", "reset", "slots", "new", "llm")

# Fixed "维持 config 配置" entry of the provider/model menus (spec §4.1: an
# empty override = resolve entirely via the yaml three-tier orchestration)
KEEP_CONFIG = "__keep_config__"


def _keep_config_entry() -> Dict[str, str]:
    return {"value": KEEP_CONFIG, "label": "（维持 config 配置）",
            "hint": "不手动指定，按 yaml pattern/module/node 配置解析"}


def _patch_select(picked: str):
    """Test hook: pin the select_from_menu return value."""
    from unittest.mock import patch as _patch
    return _patch(__name__ + ".select_from_menu", return_value=picked)


def _provider_menu_entries() -> List[Dict[str, str]]:
    return [_keep_config_entry()] + [
        {"value": p.code, "label": f"{p.code} — {p.name}",
         "hint": getattr(p, "description", "")}
        for p in llm_registry.list_providers()
    ]


# ============================================================================
# prompt_toolkit interaction: arrow-key menu + main input line
# ============================================================================

def _ptk_import():
    try:
        from prompt_toolkit import PromptSession as PtkPromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import HTML
        return PtkPromptSession, Completer, Completion, HTML
    except ImportError:
        return None, None, None, None


def select_from_menu(title: str, entries: List[Dict[str, str]]) -> Optional[str]:
    """Inline arrow-key selector; falls back to number input on unavailability,
    non-TTY, or cancel.

    radiolist_dialog is not used: in its full-screen dialog Enter only marks the
    selection — you must Tab to the Ok button and press Enter again to close,
    which is exactly why the first Enter felt "stuck". A custom Application:
    ↑↓ to move, Enter returns the selected value directly, Esc/interrupt cancels.

    entries: [{value, label, hint}]; returns the value, or None (cancelled).
    """
    mods = _ptk_import()
    if mods[0] is None or not (sys.stdout.isatty() and sys.stdin.isatty()):
        return _select_by_number(title, entries)
    PtkPromptSession, _, _, HTML = mods

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.formatted_text import HTML, to_formatted_text

    state = {"index": 0}

    def _render():
        parts = [f"<b>{title}</b>\n",
                 "<ansibrightblack>↑↓ 选择，Enter 确认，Esc 取消</ansibrightblack>\n"]
        for i, e in enumerate(entries):
            mark = "›" if i == state["index"] else " "
            hint = (f"  <ansibrightblack>{e['hint']}</ansibrightblack>"
                    if e.get("hint") else "")
            line = f"{mark} {i + 1}. {e['label']}{hint}"
            if i == state["index"]:
                line = f"<ansicyan><b>{line}</b></ansicyan>"
            parts.append(line + "\n")
        # FormattedTextControl callables must return flat fragments: convert once, wholesale
        return to_formatted_text(HTML("".join(parts)))

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["index"] = max(0, state["index"] - 1)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        state["index"] = min(len(entries) - 1, state["index"] + 1)

    @kb.add("enter")
    def _accept(event):
        event.app.exit(result=entries[state["index"]]["value"])

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    # Dynamic re-rendering: FormattedTextControl.text accepts a callable
    body = FormattedTextControl(lambda: _render())
    app = Application(
        layout=Layout(HSplit([Window(content=body, dont_extend_height=True,
                                     wrap_lines=True)])),
        key_bindings=kb,
        full_screen=False,
    )
    result = app.run()
    return result


def _select_by_number(title: str, entries: List[Dict[str, str]]) -> Optional[str]:
    """Numbered-menu fallback path (no prompt_toolkit / non-TTY). EOF counts as cancel."""
    print(render_pattern_menu(title.replace("选择 ", ""), [
        type("P", (), {"code": e["value"], "name": e["label"],
                       "description": e.get("hint", "")})() for e in entries
    ]))
    while True:
        try:
            raw = input("输入编号（回车取消）: ").strip()
        except EOFError:
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return entries[int(raw) - 1]["value"]
        print(red(f"无效编号: {raw}"))


class SlashCompleter:
    """Slash-command completion for the main input line."""

    def __init__(self):
        mods = _ptk_import()
        self._Completer = mods[1]
        self._Completion = mods[2]

    def get_completer(self):
        if self._Completer is None:
            return None

        completer_self = self

        class _Impl(self._Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                parts = text.split(maxsplit=1)
                cmd = parts[0][1:].lower()
                for name in _SLASH_COMMANDS:
                    if name.startswith(cmd):
                        yield completer_self._Completion(
                            name, start_position=-len(cmd)
                        )

        return _Impl()


# ============================================================================
# Session assembly (mirrors the binding flow of main.py _launch_session_core,
# without depending on FastAPI)
# ============================================================================

def build_session(session_id: str, pattern_code: str,
                  llm_overrides: Optional[Dict[str, Any]] = None,
                  task_info: Optional[Dict[str, str]] = None) -> Session:
    """Create a Session and complete pattern/llm binding.

    When llm_overrides is non-empty, preset metadata.llm_override (still wins
    under per-turn refresh); model is required (each stage subscripts
    llm_config["model"] directly). task_info mirrors the double write of
    main.py _launch_session_core: session.task_info (for persistence) +
    cxt.metadata["task_info"] (for the prompt slot {__task_info__}).
    """
    pattern = pattern_registry.get(pattern_code)
    if pattern is None:
        raise SystemExit(red(f"pattern '{pattern_code}' 未注册，可用: "
                             f"{pattern_registry.list_codes()}"))

    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.pattern = pattern
    session.task_info = task_info or {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    if task_info:
        session.cxt.metadata["task_info"] = task_info

    if llm_overrides:
        # Only write the truthy fields the user explicitly picked; an empty
        # override (e.g. "维持 config 配置") must not write llm_override —
        # otherwise the empty dict is still truthy and pins the global snapshot,
        # disabling the three-tier override + hot reload entirely
        # (spec §4.1: empty override = resolve from yaml)
        picked = {k: v for k, v in llm_overrides.items() if v}
        if picked:
            session.cxt.metadata["llm_override"] = picked

    return session


def resolve_llm_choice(llm: str, model: str,
                       interactive: bool = True) -> Dict[str, Any]:
    """Resolve the --llm/--model flags and interactive menus into an llm_overrides dict.

    Returns {code, model} (either may be an empty string = use yaml defaults).
    """
    code = llm or ""
    if not code and interactive:
        # When yaml already configures a provider, silently keep its default
        # (the menu only serves users who want to switch); pop the menu only
        # when nothing is configured
        try:
            code = get_llm_config().get("code") or ""
        except Exception:
            code = ""
        if code:
            return {"code": "", "model": ""}  # empty override = use yaml defaults entirely
        if llm_registry.list_providers():
            picked = select_from_menu("选择 LLM provider", _provider_menu_entries())
            if picked == KEEP_CONFIG:
                return {"code": "", "model": ""}
            code = picked or ""

    resolved_model = model or ""
    if code and not resolved_model and interactive:
        entry = llm_registry.get(code)
        models = list(getattr(entry, "models", None) or [])
        if models:
            entries = [_keep_config_entry()] + [
                {"value": m, "label": m} for m in models]
            picked = select_from_menu(f"选择 model（{code}）", entries) or ""
            if picked == KEEP_CONFIG:
                resolved_model = ""
            else:
                resolved_model = picked
        else:
            hint = f"（回车用 {getattr(entry, 'default_model', '') or '默认'}）"
            resolved_model = input(f"输入 model 名 {hint}: ").strip()
            if resolved_model == KEEP_CONFIG:
                resolved_model = ""

    return {"code": code, "model": resolved_model}


def parse_task_info(raw: Any) -> Optional[Dict[str, str]]:
    """Parse the task_info from --task-info / the input prompt into a dict.

    The input may be a JSON string (input() prompt) or a dict — fire
    auto-evaluates --task-info '{...}' into a Python dict literal.
    Empty input → None (unset); invalid JSON / non-dict → SystemExit.
    Values are coerced to str (the launch contract is Dict[str, str], and what
    the xianyu channel maps out is str too).
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(red(f"task_info 不是合法 JSON: {e}"))
    if not isinstance(data, dict):
        raise SystemExit(red(f"task_info 必须是 JSON object，得到: {type(data).__name__}"))
    return {str(k): str(v) for k, v in data.items()}


# Pattern-level mock task_info (demo/integration-debugging preset: pressing Enter
# after picking the pattern adopts it directly; --task-info or hand-typed JSON
# takes precedence). account_id aligns with the default scope (xianyu:demo) of
# cli.py knowledge-seed — customer_agent's catalog prefetch / knowledge tools
# then pick up the seed data.
MOCK_TASK_INFO: Dict[str, Dict[str, str]] = {
    "customer_agent": {"channel": "xianyu", "account_id": "demo"},
}


def mock_task_info_for(pattern_code: str) -> Optional[Dict[str, str]]:
    """Return a copy of the pattern's mock task_info (None when no preset)."""
    preset = MOCK_TASK_INFO.get(pattern_code)
    return dict(preset) if preset else None


def prompt_task_info(pattern_code: str, preset: str = "") -> Optional[Dict[str, str]]:
    """task_info input prompt shown after picking a pattern.

    - preset non-empty (--task-info flag): parse only, never ask (highest precedence)
    - pattern has a mock preset: the hint echoes the mock content; Enter/EOF adopts it
    - no mock: Enter skips (original behavior); hand-typed JSON always beats the mock
    """
    if preset:
        return parse_task_info(preset)
    mock = mock_task_info_for(pattern_code)
    hint = (f"，回车用 mock {json.dumps(mock, ensure_ascii=False)}" if mock
            else "（回车跳过）")
    try:
        raw = input(dim(f"task_info JSON{hint} [{pattern_code}]: ")).strip()
    except EOFError:
        return mock
    if not raw:
        if mock is not None:
            print(dim("  mock task_info 已应用；商品目录/知识检索需先跑 "
                      "`cli.py knowledge-seed`（默认 scope xianyu:demo）"))
        return mock
    return parse_task_info(raw)


# ============================================================================
# Turn execution + persistence (SessionStore shares the same db with the web service)
# ============================================================================

def _snapshot(cxt) -> Dict[str, Any]:
    nlu = cxt.nlu_result or {}
    return {
        "current_module_code": cxt.current_module_code,
        "current_node_code": cxt.current_node_code,
        "filled_slots": dict(cxt.filled_slots or {}),
        "intent": nlu.get("intent"),
        "next_node": nlu.get("next_node"),
    }


def run_turn(session: Session, query: str, sessions: Dict[str, Session],
             store: Optional[SessionStore], verbose: int = 0) -> str:
    """Run one dialogue turn: snapshot → chat() → end-of-turn snapshot write-back → verbose rendering.

    TODO(phase4): temporary asyncio.run bridge — the engine core is async
    since phase-2; the CLI gets its single persistent loop in phase-4.
    """
    before = _snapshot(session.cxt)

    reply = asyncio.run(chat_turn(query, session.session_id, sessions, store=store))

    if store is not None:
        try:
            store.save_snapshot(session)
        except Exception:
            logging.getLogger(__name__).exception("轮末快照失败（不影响对话）")

    if verbose >= 1:
        summary = render_verbose_summary(before, _snapshot(session.cxt))
        if summary:
            print(summary)
    if verbose >= 2:
        full = render_verbose_full(session.cxt)
        if full.strip():
            print(full)
    return reply


def _open_store(persist) -> Optional[SessionStore]:
    # fire parses --persist=false into the string 'false' (truthy!); normalize:
    if persist in (False, "false", "False", "0", 0, None):
        return None
    try:
        return SessionStore(get_session_db_path())
    except Exception:
        logging.getLogger(__name__).exception("会话存储不可用，降级为内存态")
        return None


def _find_or_create(session_id: str, pattern_code: str,
                    llm_overrides: Dict[str, Any],
                    store: Optional[SessionStore],
                    sessions: Dict[str, Session],
                    task_info: Optional[Dict[str, str]] = None) -> Session:
    """Restore and continue when --session-id matches an unexpired session in the db; otherwise create new and persist."""
    if store is not None:
        for restored, _ in store.load_active_sessions(ttl_seconds=7 * 24 * 3600):
            if restored.session_id == session_id:
                pattern = pattern_registry.get(restored.pattern_code)
                if pattern is None:
                    print(yellow(f"会话 {session_id} 的 pattern "
                                 f"'{restored.pattern_code}' 未注册，新建会话"))
                    break
                restored.pattern = pattern
                restored.cxt.module_map = pattern.module_map
                restored.cxt.node_map = pattern.node_map
                if llm_overrides:
                    picked = {k: v for k, v in llm_overrides.items() if v}
                    if picked:
                        restored.cxt.metadata["llm_override"] = picked
                print(green(f"已恢复会话 {session_id} "
                            f"({restored.pattern_code})，继续对话"))
                store.attach(restored)  # re-attach write-through for the restored session
                sessions[session_id] = restored
                return restored

    session = build_session(session_id, pattern_code, llm_overrides, task_info)
    sessions[session_id] = session
    if store is not None:
        store.create_session(session)
        store.attach(session)
    return session


# ============================================================================
# REPL
# ============================================================================

HELP_TEXT = """\
命令:
  /help            显示本帮助
  /exit            退出（Ctrl-D 同效）
  /reset           重置当前会话（同 pattern 重新开始）
  /slots           显示当前 slots / node / module 状态
  /new [pattern]   换 pattern 新会话（无参数出选择菜单）
  /llm [code]      切换 LLM（无参数出选择菜单，只影响后续轮次）\
"""


def _prompt_text(session: Session):
    """REPL prompt. The prompt_toolkit path returns formatted_text (coloring built
    in; raw ANSI codes are not parsed — a str would render \\033[...] verbatim as
    mojibake); the input() fallback path returns an ANSI-coded str."""
    cfg = session.cxt.llm_config or {}
    model = cfg.get("model") or "默认model"
    text = f"你 ({session.pattern_code}/{model})> "
    if _COLOR_OK:
        from prompt_toolkit.formatted_text import HTML
        return HTML(f"<b><ansicyan>{text}</ansicyan></b>")
    return text


def repl_loop(pattern_code: str, session_id: str, llm_overrides: Dict[str, Any],
              persist: bool, verbose: int,
              task_info: Optional[Dict[str, str]] = None) -> None:
    """Interactive chat main loop."""
    sessions: Dict[str, Session] = {}
    store = _open_store(persist)

    session = _find_or_create(session_id, pattern_code, llm_overrides,
                              store, sessions, task_info)

    PtkPromptSession = _ptk_import()[0]
    ptk_session = PtkPromptSession() if PtkPromptSession else None
    completer = SlashCompleter().get_completer()

    print(dim("输入对话内容；/help 查看命令；Ctrl-D 或 /exit 退出"))
    while True:
        try:
            if ptk_session is not None and completer is not None:
                line = ptk_session.prompt(_prompt_text(session),
                                          completer=completer)
            elif ptk_session is not None:
                line = ptk_session.prompt(_prompt_text(session))
            else:
                line = input(_prompt_text(session))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        cmd = parse_slash_command(line)
        if cmd is None:
            if not line.strip():
                continue
            reply = run_turn(session, line.strip(), sessions, store, verbose)
            print(green(f"助手: {reply}"))
            continue

        name, arg = cmd["name"], cmd["arg"]
        if name == "unknown":
            print(red(f"未知命令 /{arg}，/help 查看命令列表"))
        elif name == "help":
            print(HELP_TEXT)
        elif name == "exit":
            break
        elif name == "reset":
            session = _do_reset(session, sessions, store, verbose)
        elif name == "slots":
            _print_slots(session)
        elif name == "new":
            session = _do_new(arg, sessions, store, llm_overrides, verbose)
            if session is None:
                continue
        elif name == "llm":
            _do_llm(session, arg)

    if store is not None:
        store.close()


def _do_reset(session: Session, sessions: Dict[str, Session], store, verbose: int):
    """/reset: reopen a new session with the same pattern (keeps the original session_id, llm config and task_info)."""
    llm_override = session.cxt.metadata.get("llm_override")
    new_session = build_session(session.session_id, session.pattern_code,
                                task_info=session.task_info)
    if llm_override:
        new_session.cxt.metadata["llm_override"] = llm_override
    sessions[session.session_id] = new_session
    if store is not None:
        store.create_session(new_session)  # same id counts as a new generation (launch_epoch+1)
        store.attach(new_session)
    print(green(f"会话已重置: {session.session_id}"))
    return new_session


def _do_new(arg: str, sessions: Dict[str, Session], store, llm_overrides, verbose: int):
    """/new [pattern]: new session under a different pattern."""
    code = arg.strip() or _pick_pattern()
    if not code:
        print(yellow("未选择，保持当前会话"))
        return None
    pattern = pattern_registry.get(code)
    if pattern is None:
        print(red(f"pattern '{code}' 未注册，可用: {pattern_registry.list_codes()}"))
        return None
    new_id = f"{code}-{os.getpid()}" if store is not None else "cli"
    if new_id in sessions:
        new_id = f"{new_id}-{len(sessions)}"
    task_info = prompt_task_info(code)
    new_session = build_session(new_id, code, llm_overrides, task_info)
    sessions[new_id] = new_session
    if store is not None:
        store.create_session(new_session)
        store.attach(new_session)
    print(green(f"新会话: {new_id} ({code})"))
    return new_session


def _do_llm(session: Session, arg: str) -> None:
    """/llm [code]: switch the provider/model for subsequent turns."""
    overrides = resolve_llm_choice(arg.strip(), "", interactive=True)
    picked = {k: v for k, v in overrides.items() if v}
    if not picked:
        # "维持 config 配置": drop the override; later turns resolve via the yaml three tiers
        session.cxt.metadata.pop("llm_override", None)
        print(green("LLM 已切回 config 配置"))
        return
    # Only write explicitly picked fields; connection fields (api_base etc.)
    # always come from the llm_providers[code] section at resolution time and
    # must not be spread into the full snapshot (prevents cross-provider crosstalk)
    session.cxt.metadata["llm_override"] = picked
    print(green(f"LLM 已切换: {overrides['code']} / {overrides['model'] or '默认model'}"))


def _print_slots(session: Session) -> None:
    cxt = session.cxt
    print(dim(f"  module: {cxt.current_module_code}  node: {cxt.current_node_code}"))
    if cxt.filled_slots:
        for k, v in sorted(cxt.filled_slots.items()):
            print(dim(f"  {k} = {v!r}"))
    else:
        print(dim("  (无 slots)"))


def _pick_pattern() -> str:
    patterns = pattern_registry.list_patterns()
    if not patterns:
        print(red("没有已注册的 pattern"))
        return ""
    return select_from_menu(
        "选择 pattern",
        [{"value": p.code, "label": f"{p.code} — {p.name}",
          "hint": getattr(p, "description", "")} for p in patterns],
    ) or ""


# ============================================================================
# fire subcommands
# ============================================================================

def _ensure_discovery() -> None:
    discover_builtin_patterns()
    discover_builtin_providers()
    discover_builtin_tools()
    discover_builtin_plugins()


def chat(pattern: str = "", session_id: str = "cli", llm: str = "", model: str = "",
         task_info: str = "", verbose: int = 0, persist: bool = True) -> None:
    """Interactive REPL for exercising template dialogues.

    Args:
        pattern: pattern code (selection menu pops up when omitted)
        session_id: session id; with persistence, resumes when it matches an unexpired session in the db
        llm: provider code (selection menu pops up when omitted)
        model: model name (model menu pops up when omitted and a provider was picked)
        task_info: JSON string (input prompt after the pattern is picked when omitted; Enter skips)
        verbose: debug level 0/1/2 (-v/-vv expand automatically on the command line)
        persist: persist to data/dialogue.db (on by default)
    """
    _ensure_discovery()
    # Restore precedence: an explicit --pattern starts a new session; otherwise an
    # explicit --session-id matching an unexpired session in the db restores it
    # directly (no menu); only when both are missing does the menu pop up.
    # fire's default parameter values are indistinguishable from user-passed
    # values, so detect explicit passing via argv.
    sid_specified = any(a == "--session-id" or a.startswith("--session-id=")
                        for a in sys.argv)
    restored_code = ""
    if not pattern and sid_specified:
        store = _open_store(persist)
        if store is not None:
            try:
                for restored, _ in store.load_active_sessions(7 * 24 * 3600):
                    if restored.session_id == session_id:
                        restored_code = restored.pattern_code
                        break
            finally:
                store.close()
    pattern_code = pattern or restored_code or _pick_pattern()
    if not pattern_code:
        print(yellow("未选择 pattern，退出"))
        return
    task_info_dict = prompt_task_info(pattern_code, task_info)
    llm_overrides = resolve_llm_choice(llm, model, interactive=True)
    # Default session-id gets the pid appended: avoids matching any old session
    # in the db that would swap out the just-picked pattern
    sid = session_id if (sid_specified or pattern) else f"{session_id}-{os.getpid()}"
    repl_loop(pattern_code, sid, llm_overrides, persist, verbose, task_info_dict)


def ask(query: str, pattern: str = "", session_id: str = "cli-ask", llm: str = "",
        model: str = "", task_info: str = "", verbose: int = 0,
        persist: bool = True) -> None:
    """One-shot Q&A (--session-id resumes a session from the db)."""
    _ensure_discovery()
    pattern_code = pattern
    if not pattern_code:
        # one-shot shows no menu: when restoring an existing session the pattern
        # comes from the db; otherwise an explicit --pattern is required
        store = _open_store(persist)
        if store is not None:
            for restored, _ in store.load_active_sessions(7 * 24 * 3600):
                if restored.session_id == session_id:
                    pattern_code = restored.pattern_code
                    break
            store.close()
        if not pattern_code:
            raise SystemExit(red("ask 需要显式 --pattern，或 --session-id 命中已有会话"))
    # one-shot skips the extra prompt: parse --task-info when explicitly passed,
    # otherwise no task_info
    task_info_dict = parse_task_info(task_info)
    llm_overrides = resolve_llm_choice(llm, model, interactive=False)
    sessions: Dict[str, Session] = {}
    store = _open_store(persist)
    session = _find_or_create(session_id, pattern_code, llm_overrides,
                              store, sessions, task_info_dict)
    reply = run_turn(session, query, sessions, store, verbose)
    print(reply)
    if store is not None:
        store.close()


def list_cmd(target: str = "all") -> None:
    # Note: must not be named list — it would shadow the builtin list(), and the
    # list(...) inside this module's f-strings would become a recursive call to
    # this function. The fire subcommand name is mapped in the fire.Fire dict.
    """List registered objects: patterns | llms | tools | all."""
    _ensure_discovery()
    if target in ("tools", "all"):
        # MCP 工具是 bootstrap 后台异步注册的——观测命令等待连接终态,
        # 让列表反映真实状态(无 server 配置时立即返回)
        try:
            from atoms.mcp.manager import get_mcp_manager
            get_mcp_manager().wait_ready(timeout=15.0)
        except Exception:
            pass
    if target in ("patterns", "all"):
        patterns = pattern_registry.list_patterns()
        print(bold(f"patterns ({len(patterns)}):"))
        for p in patterns:
            print(f"  {p.code} — {p.name}")
            if getattr(p, "description", ""):
                print(dim(f"    {p.description}"))
    if target in ("llms", "all"):
        providers = llm_registry.list_providers()
        print(bold(f"llm providers ({len(providers)}):"))
        for prov in providers:
            print(f"  {prov.code} — {prov.name}")
            print(dim(f"    default_model={getattr(prov, 'default_model', '')} "
                      f"models={list(getattr(prov, 'models', None) or [])}"))
    if target in ("tools", "all"):
        names = tool_registry.get_all_tool_names()
        print(bold(f"tools ({len(names)}):"))
        for n in names:
            print(f"  {n}")


def sessions(pattern_code: str = "", limit: int = 20) -> None:
    """List persisted sessions (last_active_at descending)."""
    try:
        store = SessionStore(get_session_db_path())
    except Exception as e:
        raise SystemExit(red(f"会话存储不可用: {e}"))
    try:
        rows = store.list_sessions(pattern_code=pattern_code or None,
                                   limit=limit)
    finally:
        store.close()
    if not rows:
        print(yellow("（无会话记录）"))
        return
    print(bold(f"{'session_id':24} {'pattern':22} {'node':16} {'msgs':>4}  last_active"))
    for r in rows:
        print(f"{r['session_id']:24} {r['pattern_code']:22} "
              f"{str(r['current_node_code']):16} {r['message_count']:>4}  "
              f"{_fmt_ts(r['last_active_at'])}")


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _expand_short_verbose(argv: List[str]) -> List[str]:
    """Expand -v/-vv/-vvv into --verbose=N (fire does not support counting short flags)."""
    mapping = {"-v": "--verbose=1", "-vv": "--verbose=2", "-vvv": "--verbose=3"}
    return [mapping.get(a, a) for a in argv]


def knowledge_seed(scope: str = "xianyu:demo") -> None:
    """Seed the knowledge base with demo seed data (idempotent).

    Args:
        scope: knowledge isolation domain, format {channel}:{account_id}
    """
    from atoms.knowledge.store import close_knowledge_store, get_knowledge_store

    store = get_knowledge_store()
    try:
        store.seed(scope)
        n_products = len(store.search_products(scope, limit=50))
        n_cs = len(store.search_cs(scope, limit=50))
        print(green(f"知识库种子完成: scope={scope}, 商品 {n_products} 条, 客服知识 {n_cs} 条"))
    finally:
        close_knowledge_store()


def pattern_export(pattern: str, out: str = "") -> None:
    """Export a registered pattern to YAML (plan-③ round-trip).

    Args:
        pattern: pattern code (must be registered)
        out: output file path; prints to stdout when omitted
    """
    _ensure_discovery()
    p = pattern_registry.get(pattern)
    if p is None:
        print(red(f"pattern '{pattern}' 未注册"))
        sys.exit(1)
    from nexus.model.serialization import pattern_to_yaml
    text = pattern_to_yaml(p)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(green(f"已导出 {pattern} → {out}"))
    else:
        print(text)


def pattern_load(path: str, validate_only: bool = False) -> None:
    """Load a pattern from a YAML file: construct → validate → register.

    Args:
        path: YAML file path
        validate_only: only construct+validate, do not register
    """
    _ensure_discovery()
    from nexus.model.serialization import pattern_from_yaml
    from nexus.model.validation import validate_pattern

    text = Path(path).read_text(encoding="utf-8")
    try:
        p = pattern_from_yaml(text)
        validate_pattern(p)
    except ValueError as e:
        print(red(f"加载失败:\n{e}"))
        sys.exit(1)
    if validate_only:
        print(green(f"校验通过: {p.code}（未注册）"))
        return
    pattern_registry.register(p)
    print(green(f"已加载并注册 pattern: {p.code}（modules={len(p.module_map)}）"))


def _setup_logging() -> None:
    """按 NEXUS_LOG 环境变量配置根 logger(默认 WARNING)。

    项目各模块只 getLogger 不配 handler——CLI 不配置时 INFO/DEBUG 全部
    被吞。NEXUS_LOG=INFO / DEBUG 级别可见引擎轮次、MCP 连接、工具分派
    等过程日志;NEXUS_LOG=DEBUG 再把 httpx/httpcore/mcp.client 噪声压回
    WARNING,保持信噪比。
    """
    level_name = os.environ.get("NEXUS_LOG", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if level >= logging.DEBUG:
        for noisy in ("httpx", "httpcore", "mcp.client", "asyncio",
                      "urllib3", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    sys.argv[1:] = _expand_short_verbose(sys.argv[1:])
    _setup_logging()
    fire.Fire({
        "chat": chat,
        "ask": ask,
        "list": list_cmd,
        "sessions": sessions,
        "knowledge-seed": knowledge_seed,
        "pattern-export": pattern_export,
        "pattern-load": pattern_load,
    })
