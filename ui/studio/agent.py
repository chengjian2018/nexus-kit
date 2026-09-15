"""Studio agent —— LLM 提示词构建 / 围栏输出解析 / 生成插件验证。

自动编排与流程编排 AI 助手共用的生成层：

- ``build_generate_messages``：把用户表单（流程背景/功能/实现案例/…）+ 框架
  契约（pattern schema、TurnResult/NodeExecutor、已注册插件目录、范例 YAML）
  组装成 chat messages。系统提示词是生成质量的全部来源——schema、执行语义、
  插件契约、输出格式四块缺一不可。
- ``pick_exemplar_yaml``：从注册表现场导出一个体量最小的 agent 型 pattern 作
  格式范例（无 agent 型时退回任意最小 pattern）。
- ``parse_generation_output``：解析 LLM 输出的 ```yaml / ```python 围栏。
  开口=行首 `````lang`、闭合=独立成行的 ````` ``——字符串值里行中的围栏
  记号（如被抄进 base_prompt 的提示词原文「恰好一个 ```yaml 围栏」）不再
  提前闭合外层围栏；yaml 候选闭合级联（最短候选构造失败时逐级放宽，防
  内嵌完整围栏把 pattern 截成半份）。插件块从首行注释
  ``# studio-plugin: file=<stem>.py`` 取文件名，缺失时从首个注册声明的
  code 推导；再正则抽取全部 (kind, code) 注册声明。
- ``validate_pattern_text``：from_yaml + validate_pattern（工具面默认宽松：
  未注册/越集工具降级为 warnings——新生成的模版可能声明新工具），把收集式
  报错/软警告拆成逐条列表（前端逐行展示）。
- ``build_assist_messages``：流程编排 AI 助手（话术/回答范式/执行器）的
  提示词。

这里不做任何落盘/注册——那是 api.py 调 store.py 的事（预览态插件经
``store.import_plugin_module`` 于临时文件导入，属于注册但非持久化）。
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
# 输出围栏解析
# ---------------------------------------------------------------------------
#
# 闭合必须「独立成行」：生成的 pattern 常在字符串值（base_prompt 等）里
# 内嵌提示词原文，其中行中的 ``` 记号（「恰好一个 ```yaml 围栏」）若被
# 当作闭合，外层 yaml 围栏会被静默截断——截断处落在块标量内部时 YAML
# 仍合法，于是构造出一张缺了后半节点的图，报出难定位的悬空边。

# 围栏开口：行首（或串首）的 ```lang 换行；行中的 ``` 记号不构成开口
_FENCE_OPEN_RE = re.compile(
    r"(?:^|\n)[ \t]*```([A-Za-z0-9_+-]*)[ \t]*\r?\n")

# 围栏闭合：独立成行的 ```（前后仅限空白至行尾）。不消费行尾换行——
# 紧邻的下一个围栏开口还需要它
_FENCE_CLOSE_RE = re.compile(r"\n[ \t]*```[ \t]*(?=\r?\n|$)")

# 插件模块的注册声明：plugin_registry.register("executor", "code", Factory)
_REGISTER_DECL_RE = re.compile(
    r"plugin_registry\.register\(\s*['\"]([a-z_]+)['\"]\s*,\s*"
    r"['\"]([A-Za-z0-9_-]+)['\"]")

# 插件块首行的文件名声明：# studio-plugin: file=gen_xxx.py
_PLUGIN_FILE_RE = re.compile(
    r"^\s*#\s*studio-plugin:\s*file=([A-Za-z0-9_.-]+)", re.IGNORECASE)

# 可生成插件的合法 kind（stage_factory 是内核内部存储，不开放生成）
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
    """(open_match, closes)：从 pos 起的下一个围栏开口 + 其后全部独立成行
    闭合候选（位置升序）。closes 为空 = 该开口未闭合（尾部截断输出）。"""
    m = _FENCE_OPEN_RE.search(text, pos)
    if m is None:
        return None, []
    return m, list(_FENCE_CLOSE_RE.finditer(text, m.end()))


def _iter_fences(text: str):
    """产出 (lang, body)：开口=行首 ```lang，闭合=最近的独立成行 ```。

    语义对齐旧 ``findall`` 正则：每个围栏体取首个闭合，未闭合的尾部围栏
    整体丢弃；行中 ``` 记号既不开也不闭。"""
    pos = 0
    while True:
        m, closes = _next_fence(text, pos)
        if m is None or not closes:
            return
        yield m.group(1).lower(), text[m.end():closes[0].start()]
        pos = closes[0].end()


def _pick_pattern_yaml(text: str) -> Optional[str]:
    """提取 pattern YAML 围栏体（候选闭合级联）。

    先按现行语义取最短候选（首个独立成行闭合）；若它构造失败——围栏体
    内嵌了独立成行的 ``` 围栏（提示词范例被整个抄进 base_prompt）把
    pattern 截成半份——则逐级放宽到更长候选，取第一个能完整构造的。
    全部失败时返回最短候选，具体错误交给上层校验去报。"""
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
    """解析一个插件围栏块：文件名 + 全部 (kind, code) 注册声明。"""
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
    """解析 LLM 完整输出：{yaml, plugins[], fenced_count}。

    宽容策略：语言标签缺失时按内容启发式判定（yaml 以 code: 起头 /
    python 含 plugin_registry.register）；多个 yaml 围栏取第一个，
    其余忽略（模型偶发复读）。围栏开闭的行级规则与 yaml 候选级联见
    ``_iter_fences`` / ``_pick_pattern_yaml``。
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
# 校验
# ---------------------------------------------------------------------------

def validate_pattern_text(yaml_text: str, strict_tools: bool = False
                          ) -> Tuple[Optional[Pattern], List[str], List[str]]:
    """from_yaml + validate_pattern；返回 (pattern, errors, warnings)。

    pattern 非 None 但 errors 非空 = 构造成功、校验失败（meta 仍可展示）；
    pattern None = YAML/构造期就失败。

    默认宽松工具校验（strict_tools=False）：生成工作台的产物可能引用
    尚未注册的新工具（新生成模版声明新 tool 是常态），未注册/越集降级为
    warnings 透出给预览，不阻塞应用；运行期由 deny-by-default 解析兜底。
    """
    try:
        pattern = pattern_from_yaml(yaml_text)
    except ValueError as e:
        return None, [str(e)], []
    except Exception as e:  # yaml 语法错等
        return None, [f"YAML 解析失败: {e}"], []
    warnings: List[str] = ([] if strict_tools
                           else tool_check_notices(pattern))
    try:
        validate_pattern(pattern, strict_tools=strict_tools)
    except ValueError as e:
        return pattern, _split_validation_errors(str(e)), warnings
    return pattern, [], warnings


def _split_validation_errors(message: str) -> List[str]:
    """把 validate_pattern 的编号多行报错拆成逐条列表。"""
    lines = [ln.strip() for ln in message.splitlines()]
    items = [ln for ln in lines if re.match(r"^\[\d+\]", ln)]
    return items or [message]


def declared_codes_registered(declarations) -> List[str]:
    """返回尚未注册的 (kind, code) 描述（空列表 = 全部就位）。"""
    missing = []
    for kind, code in declarations:
        if not plugin_registry.has(kind, code):
            missing.append(f"{kind}:{code}")
    return missing


# ---------------------------------------------------------------------------
# 范例与目录
# ---------------------------------------------------------------------------

def pick_exemplar_yaml() -> str:
    """从注册表现场导一个体量最小的 pattern YAML 作格式范例。"""
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
        text = text[:_EXEMPLAR_MAX_CHARS] + "\n# （超长截断，仅作格式参照）"
    return text


def plugin_catalog() -> Dict[str, List[str]]:
    """按 kind 列出已注册插件 code（提示词里引导优先复用）。"""
    return {
        kind: plugin_registry.list_codes(kind)
        for kind in ("executor", "stage", "messages_builder")
    }


# ---------------------------------------------------------------------------
# 自动编排：提示词
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
    """组装自动编排的 chat messages（系统提示词 + 用户需求）。"""
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
# 流程编排 AI 助手：提示词
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
    """组装 AI 助手 messages；mode ∈ node_prompt / node_examples /
    plugin_generate，payload 为节点上下文 + hints。"""
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
    """解析助手输出的 ```json 围栏（宽容：无围栏时尝试整段 JSON）。"""
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
