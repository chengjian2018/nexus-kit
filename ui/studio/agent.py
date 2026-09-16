"""Studio agent — LLM prompt building / fence output parsing / generated-plugin validation.

The generation layer shared by the auto-orchestration and flow-orchestration
AI assistants:

- ``build_generate_messages``: assembles the user form (flow background /
  features / implementation examples / ...) plus the framework contract
  (pattern schema, TurnResult/NodeExecutor, the registered plugin catalog,
  exemplar YAML) into chat messages. The system prompt is the sole source of
  generation quality — schema, execution semantics, plugin contracts, and
  output format: all four are indispensable.
- ``pick_exemplar_yaml``: exports on the spot the smallest agent-type pattern
  from the registry as a format exemplar (falls back to any minimal pattern
  when no agent-type one exists).
- ``parse_generation_output``: parses the ```yaml / ```python fences the LLM
  emits. Opening = `````lang` at line start, closing = a standalone ````` `` —
  fence markers mid-line inside string values (e.g. prompt text copied
  verbatim into base_prompt containing "exactly one ```yaml fence") no longer
  close the outer fence prematurely; yaml candidates close in a cascade (the
  shortest candidate is relaxed level by level on construction failure, so an
  embedded complete fence cannot cut the pattern in half). The plugin block
  takes its filename from the first-line comment
  ``# studio-plugin: file=<stem>.py``, falling back to the first registered
  declaration's code when missing; then all (kind, code) registration
  declarations are extracted via regex.
- ``validate_pattern_text``: from_yaml + validate_pattern (the tool surface
  defaults to lenient: unregistered/out-of-set tools degrade to warnings —
  freshly generated templates may declare new tools), splitting the collected
  errors/soft warnings into per-item lists (displayed line by line in the
  frontend).
- ``build_assist_messages``: prompts for the flow-orchestration AI assistant
  (node scripts / answer examples / executors).

Nothing here persists to disk or registers — that is api.py calling store.py
(preview-state plugins are imported from a temporary file through
``store.import_plugin_module``: registered, but not persisted).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from nexus.model.pattern import Pattern
from nexus.model.serialization import (
    pattern_from_yaml,
    pattern_to_yaml,
)
from nexus.model.validation import tool_check_notices, validate_pattern
from nexus.registry.patterns import registry as pattern_registry
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output fence parsing
# ---------------------------------------------------------------------------
#
# The close must "stand alone on its line": generated patterns often embed
# prompt text verbatim inside string values (base_prompt etc.), where an
# inline ``` marker (from "exactly one ```yaml fence") would — if treated as
# a close — silently truncate the outer yaml fence. When the truncation
# lands inside a block scalar the YAML still parses, yielding a graph
# missing its second half that fails with a hard-to-locate dangling edge.

# Fence open: a line-start (or string-start) ```lang followed by a newline;
# an inline ``` marker does not open
_FENCE_OPEN_RE = re.compile(
    r"(?:^|\n)[ \t]*```([A-Za-z0-9_+-]*)[ \t]*\r?\n")

# Fence close: a ``` standing alone on its line (only whitespace before it
# through end of line). The trailing newline is not consumed — the very next
# fence open needs it
_FENCE_CLOSE_RE = re.compile(r"\n[ \t]*```[ \t]*(?=\r?\n|$)")

# A plugin module's registration declaration: plugin_registry.register("executor", "code", Factory)
_REGISTER_DECL_RE = re.compile(
    r"plugin_registry\.register\(\s*['\"]([a-z_]+)['\"]\s*,\s*"
    r"['\"]([A-Za-z0-9_-]+)['\"]")

# The plugin block's first-line filename declaration: # studio-plugin: file=gen_xxx.py
_PLUGIN_FILE_RE = re.compile(
    r"^\s*#\s*studio-plugin:\s*file=([A-Za-z0-9_.-]+)", re.IGNORECASE)

# Legal plugin kinds for generation (stage_factory is internal kernel storage, not open for generation)
GENERATABLE_PLUGIN_KINDS = ("executor", "stage", "messages_builder",
                            "agent_hooks")

_EXEMPLAR_MAX_CHARS = 6000


def _looks_like_pattern_yaml(text: str) -> bool:
    head = text.lstrip()[:200]
    return head.startswith("code:") or "\ncode:" in head


def _looks_like_python(text: str) -> bool:
    return "plugin_registry.register" in text or text.lstrip().startswith(
        ("import ", "from ", "class ", "def "))


def _next_fence(text: str, pos: int):
    """(open_match, closes): the next fence open from pos plus every standalone-line close candidate after it (ascending position).
    Empty closes = the open is unclosed (a tail-truncated output)."""
    m = _FENCE_OPEN_RE.search(text, pos)
    if m is None:
        return None, []
    return m, list(_FENCE_CLOSE_RE.finditer(text, m.end()))


def _iter_fences(text: str):
    """Yield (lang, body): open = line-start ```lang, close = the nearest standalone-line ```.

    Semantics aligned with the old ``findall`` regex: each fence body takes its first close, an unclosed tail fence
    is dropped whole; an inline ``` marker neither opens nor closes."""
    pos = 0
    while True:
        m, closes = _next_fence(text, pos)
        if m is None or not closes:
            return
        yield m.group(1).lower(), text[m.end():closes[0].start()]
        pos = closes[0].end()


def _pick_pattern_yaml(text: str) -> Optional[str]:
    """Extract the pattern-YAML fence body (cascading close candidates).

    First take the shortest candidate per current semantics (the first standalone-line close); if constructing from
    it fails — the fence body embeds its own standalone-line ``` fence (a prompt exemplar copied whole into
    base_prompt) splitting the pattern in half — relax to longer candidates step by step, taking the first that
    constructs completely. When everything fails, return the shortest candidate and let the upper validation layer
    report the specific error."""
    pos = 0
    while True:
        m, closes = _next_fence(text, pos)
        if m is None or not closes:
            return None
        tag = m.group(1).lower()
        head = text[m.end():closes[0].start()].strip("\r\n")
        if head.strip() and (tag in ("yaml", "yml")
                             or (not tag and _looks_like_pattern_yaml(head))):
            for close in closes:
                body = text[m.end():close.start()].strip("\r\n")
                if not body.strip():
                    continue
                try:
                    pattern_from_yaml(body)
                    return body
                except Exception:
                    continue
            return head
        pos = closes[0].end()


def parse_plugin_block(code_text: str) -> Dict[str, Any]:
    """Parse one plugin fence block: the filename + all (kind, code) registration declarations."""
    filename = None
    match = _PLUGIN_FILE_RE.match(code_text)
    if match:
        filename = match.group(1)
    declarations = [
        (kind, code)
        for kind, code in _REGISTER_DECL_RE.findall(code_text)
        if kind in GENERATABLE_PLUGIN_KINDS
    ]
    stem = None
    if filename:
        stem = re.sub(r"\.py$", "", filename)
    elif declarations:
        stem = declarations[0][1]
    if stem:
        stem = stem.lower()
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", stem):
            stem = None
    return {
        "code_text": code_text,
        "filename": f"{stem}.py" if stem else None,
        "stem": stem,
        "declarations": declarations,
    }


def parse_generation_output(text: str) -> Dict[str, Any]:
    """Parse the LLM's full output: {yaml, plugins[], fenced_count}.

    Lenient policy: a missing language tag is decided by content heuristics
    (yaml starts with code: / python contains plugin_registry.register); of
    several yaml fences the first is taken and the rest ignored (an
    occasional model stutter). The line-level fence open/close rules and the
    yaml candidate cascade live in ``_iter_fences`` / ``_pick_pattern_yaml``.
    """
    yaml_text = _pick_pattern_yaml(text or "")
    plugins: List[Dict[str, Any]] = []
    fenced = 0
    for lang, body in _iter_fences(text or ""):
        body = body.strip("\r\n")
        if not body.strip():
            continue
        fenced += 1
        if lang in ("python", "py") or (not lang and _looks_like_python(body)):
            plugins.append(parse_plugin_block(body))
    return {"yaml": yaml_text, "plugins": plugins, "fenced_count": fenced}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_pattern_text(yaml_text: str, strict_tools: bool = False
                          ) -> Tuple[Optional[Pattern], List[str], List[str]]:
    """from_yaml + validate_pattern; returns (pattern, errors, warnings).

    pattern non-None but errors non-empty = constructed OK, validation
    failed (the meta can still be displayed); pattern None = YAML/construction
    already failed.

    Lenient tool validation by default (strict_tools=False): generation
    workbench artifacts may reference not-yet-registered new tools (a fresh
    template declaring a new tool is normal); unregistered/cross-toolset
    findings downgrade to warnings surfaced in the preview without blocking
    apply; runtime deny-by-default resolution is the backstop.
    """
    try:
        pattern = pattern_from_yaml(yaml_text)
    except ValueError as e:
        return None, [str(e)], []
    except Exception as e:  # e.g. a YAML syntax error
        return None, [f"YAML 解析失败: {e}"], []
    warnings: List[str] = ([] if strict_tools
                           else tool_check_notices(pattern))
    try:
        validate_pattern(pattern, strict_tools=strict_tools)
    except ValueError as e:
        return pattern, _split_validation_errors(str(e)), warnings
    return pattern, [], warnings


def _split_validation_errors(message: str) -> List[str]:
    """Split validate_pattern's numbered multi-line error into a per-item list."""
    lines = [ln.strip() for ln in message.splitlines()]
    items = [ln for ln in lines if re.match(r"^\[\d+\]", ln)]
    return items or [message]


def declared_codes_registered(declarations) -> List[str]:
    """Return the not-yet-registered (kind, code) descriptions (empty list = all in place)."""
    missing = []
    for kind, code in declarations:
        if not plugin_registry.has(kind, code):
            missing.append(f"{kind}:{code}")
    return missing


# ---------------------------------------------------------------------------
# Exemplars and catalog
# ---------------------------------------------------------------------------

def pick_exemplar_yaml() -> str:
    """Export the smallest live registered pattern YAML as a format exemplar."""
    patterns = pattern_registry.list_patterns()
    if not patterns:
        return ""
    agents = [p for p in patterns if p.pattern_type == "agent"]
    pool = agents or patterns
    chosen = min(pool, key=lambda p: len(p.node_map))
    try:
        text = pattern_to_yaml(chosen)
    except Exception:
        logger.exception("范例 pattern 导出失败: %s", chosen.code)
        return ""
    if len(text) > _EXEMPLAR_MAX_CHARS:
        text = text[:_EXEMPLAR_MAX_CHARS] + "\n# (truncated for length, format reference only)"
    return text


def plugin_catalog() -> Dict[str, List[str]]:
    """List registered plugin codes by kind (the prompts steer toward reuse first)."""
    return {
        kind: plugin_registry.list_codes(kind)
        for kind in ("executor", "stage", "messages_builder")
    }


# ---------------------------------------------------------------------------
# Auto-orchestration: prompts
# ---------------------------------------------------------------------------

_GENERATE_SYSTEM_PROMPT = """你是 nexus-kit 的 pattern / 插件生成 agent。nexus-kit 是一个声明式对话 Agent 编排框架：pattern（二层模型 Pattern → BaseNode）声明一张对话流图，运行时按 pattern_type 分流执行。

【Pattern YAML schema】（与构造参数一致，全字段 str/bool/list/dict）
顶层字段：
- code: 唯一编码（小写字母开头，小写字母/数字/下划线）
- name / description: 名称与描述
- pattern_type: "agent"（默认推荐——图运行时）| "fsm"（固定状态机+管线骨架）
- entry_node_code: 入口节点 code
- nodes: 节点列表（inline dict）
- allow_toolset: 工具集授权（deny-by-default；不用工具则省略）
- plugins: {loop/fsm/messages_builder/agent_hooks: code}（可省略，落默认执行器；LLM 选择不经 plugins，由配置层 llm_default/app config 决定）
- max_steps: agent 图每轮最大步数（默认 10，可省略）
节点字段：
- code / name / description（场景描述，喂 NLG）/ task_description（待办描述，喂 NLU）
- sub_nodes: 后继节点 code 列表（agent=图邻接边；fsm=合法转移集）
- answer_examples: 回答范式示例列表（prompt 资产）
- stages / slots: 仅 fsm pattern 可用
- use_tools: 工具名列表（空=禁用一切工具，deny-by-default）
- is_end: 终态标记
- plugins: 节点级插件槽位（压 pattern 层）
- config: 自由声明袋，提示词资产放这里（base_prompt 等）

【执行语义】
- agent 图：每条用户消息从 entry 起跑图；每步解析节点执行器：node.plugins["loop"] > pattern.plugins["loop"] > default_loop（内置 ReAct 工具循环）
- 绝大多数节点不需要自定义执行器——给足 description / task_description / config.base_prompt，default_loop 就能驱动
- 仅当需要本地规则路由、运行时扇出、特殊协议时才生成自定义执行器插件

【自定义执行器插件契约】（确有必要才生成，能复用已注册插件就不要生成）
- 类继承 NodeExecutor，无状态（一切状态在 ec.cxt 上）；异步方法：
  async def execute(self, ec) -> TurnResult
- 导入：from nexus.engine.execution import ExecutionContext, NodeExecutor
        from nexus.engine.turn_result import TurnResult, Send
- TurnResult 字段：content=本轮回复文本（空=静默中间节点）；next=条件边目标
  （必须属于本节点 sub_nodes）；sends=[Send(node_code, input)] 运行时扇出；
  wait_human=True 挂起等待用户下一条消息（重入时 ec.resume_input 为该消息）
- LLM 调用习语：
  from nexus.llm.resolve import build_provider
  llm_config = ec.cxt.llm_config or {}
  provider = build_provider(llm_config)
  result = await provider.achat_completion(
      messages=[...], model=llm_config.get("model"),
      temperature=llm_config.get("temperature", 0.7),
      max_tokens=llm_config.get("max_tokens", 2048))
  text = result.get("content", "")
- 模块末尾必须自注册：
  from nexus.registry.plugins import registry as plugin_registry
  plugin_registry.register("executor", "<code>", <Factory类>)
- 插件 code 全局唯一，建议带业务前缀（如 gen_）

【优先复用已注册插件】（plugins 槽位引用这些 code，不要重复造）
executor: __EXECUTOR_CODES__
stage: __STAGE_CODES__
messages_builder: __MESSAGES_BUILDER_CODES__

【范例 pattern YAML】（注册表现场导出，仅作格式参照，不要照抄内容）
```yaml
__EXEMPLAR_YAML__
```

【输出格式（严格遵守）】
1. 先用三到五句话说明设计：节点划分、路由逻辑、是否生成插件及理由
2. 恰好一个 ```yaml 围栏：完整 pattern YAML（顶层 code/name/description/pattern_type/entry_node_code/nodes 必备）
3. 0 到 N 个 ```python 围栏：每个是一个可直接落盘的插件模块，首行注释
   `# studio-plugin: file=<stem>.py`（stem 小写下划线），模块末尾自注册
4. 不要输出其它围栏代码块；YAML 不写注释性空壳字段"""


def build_generate_messages(req: Dict[str, Any]) -> List[Dict[str, str]]:
    """Assemble the auto-orchestration chat messages (system prompt + the user's requirement)."""
    catalog = plugin_catalog()
    system = (_GENERATE_SYSTEM_PROMPT
              .replace("__EXECUTOR_CODES__", ", ".join(catalog["executor"]) or "（无）")
              .replace("__STAGE_CODES__", ", ".join(catalog["stage"]) or "（无）")
              .replace("__MESSAGES_BUILDER_CODES__",
                       ", ".join(catalog["messages_builder"]) or "（无）")
              .replace("__EXEMPLAR_YAML__",
                       pick_exemplar_yaml() or "（注册表为空，按 schema 生成）"))
    parts: List[str] = []
    if req.get("name"):
        parts.append(f"模版名称：{req['name']}")
    if req.get("code"):
        parts.append(f"模版 code：{req['code']}")
    parts.append(f"【流程背景】\n{req['background']}")
    features = req.get("features") or ""
    feature_lines = "\n".join(
        f"- {ln.strip()}" for ln in features.splitlines() if ln.strip())
    parts.append(f"【功能清单】\n{feature_lines}")
    if req.get("examples"):
        parts.append(f"【实现案例】\n{req['examples']}")
    if req.get("extra"):
        parts.append(f"【补充要求】\n{req['extra']}")
    parts.append(
        "请按输出格式给出设计说明、pattern YAML，以及（确有必要时的）插件模块。")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# Flow-editing AI assistant: prompts
# ---------------------------------------------------------------------------

_ASSIST_MODES = {
    "node_prompt": (
        "你是 nexus-kit 的节点话术撰写助手。根据节点信息与补充要点，"
        "为一个对话节点撰写 system 提示词与回答范式。"
        "只输出一个 ```json 围栏，结构："
        '{"base_prompt": "string（system 提示词，含角色/目标/约束/风格）", '
        '"answer_examples": ["范式1", "范式2"]}'),
    "node_examples": (
        "你是 nexus-kit 的回答范式撰写助手。根据节点信息与补充要点撰写 3-5 条"
        "回答范式示例。只输出一个 ```json 围栏，结构："
        '{"answer_examples": ["范式1", "范式2", "范式3"]}'),
    "plugin_generate": (
        "你是 nexus-kit 的插件生成助手。根据需求为一个对话节点生成自定义执行器"
        "插件模块（NodeExecutor 子类，契约如下）：\n"
        "- async def execute(self, ec) -> TurnResult；无状态，状态在 ec.cxt\n"
        "- from nexus.engine.execution import ExecutionContext, NodeExecutor\n"
        "- from nexus.engine.turn_result import TurnResult, Send\n"
        "- TurnResult: content=回复文本；next=条件边目标（必须 ⊆ 节点 sub_nodes）；"
        "sends=扇出；wait_human=挂起等用户\n"
        "- LLM 调用：build_provider(ec.cxt.llm_config or {}) → await "
        "provider.achat_completion(messages=..., model=cfg.get(\"model\"), ...) → "
        "result.get(\"content\", \"\")\n"
        "- 模块末尾自注册：from nexus.registry.plugins import registry as "
        "plugin_registry; plugin_registry.register(\"executor\", \"<code>\", "
        "<Factory类>)\n"
        "只输出一个 ```python 围栏，首行注释 `# studio-plugin: file=<stem>.py`，"
        "插件 code 用 gen_ 前缀。优先判断是否可用已注册插件直接替代——可以替代就"
        "输出说明文字而不要生成代码。"),
}


def build_assist_messages(mode: str, payload: Dict[str, Any],
                          ) -> List[Dict[str, str]]:
    """Assemble the AI-assistant messages; mode ∈ node_prompt / node_examples /
    plugin_generate, payload = node context + hints."""
    system = _ASSIST_MODES.get(mode)
    if system is None:
        raise ValueError(
            f"未知 assist mode: {mode!r}（合法: {sorted(_ASSIST_MODES)}）")
    parts: List[str] = []
    for key in ("pattern_code", "node_code", "node_name", "description",
                "task_description", "sub_nodes"):
        if payload.get(key):
            parts.append(f"{key}: {payload[key]}")
    if payload.get("node_config_prompt"):
        parts.append(f"现有 base_prompt:\n{payload['node_config_prompt']}")
    if payload.get("hints"):
        parts.append(f"【补充要点】\n{payload['hints']}")
    parts.append("请严格按要求的输出格式作答。")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_assist_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the assistant's ```json fence (lenient: without a fence, try the whole text as JSON)."""
    for _, body in _iter_fences(text or ""):
        if body.strip():
            try:
                import json
                return json.loads(body)
            except ValueError:
                continue
    try:
        import json
        return json.loads((text or "").strip())
    except ValueError:
        return None
