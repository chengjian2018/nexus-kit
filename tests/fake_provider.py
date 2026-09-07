"""Scripted FakeProvider — fake LLM provider shared by offline tests (no real API access).

Distinguishes unified-stage / NLU / NLG / retry requests by prompt content and
returns fixed results, reused by the route pattern's logic and API tests.
"""

import json

from nexus.registry.providers import registry as llm_registry
from nexus.llm.provider import BaseLLMProvider

FAKE_PROVIDER_CODE = "fake_test_provider"


class FakeProvider(BaseLLMProvider):
    """Offline scripted LLM provider."""

    call_count = 0

    def _chat_completion_impl(
        self,
        messages,
        model,
        temperature,
        max_tokens,
        stream=False,
        **kwargs,
    ):
        type(self).call_count += 1
        prompt = messages[0]["content"]
        return {"content": scripted_response(prompt)}


def register_fake_provider() -> None:
    """Register the scripted provider with the LLM registry (idempotent)."""
    if not llm_registry.is_registered(FAKE_PROVIDER_CODE):
        llm_registry.register(
            code=FAKE_PROVIDER_CODE,
            name="FakeProvider",
            description="offline scripted provider for route pattern tests",
            provider_class=FakeProvider,
            default_model="fake-model",
        )


def fake_llm_config() -> dict:
    """Return an llm_config that uses FakeProvider."""
    return {
        "code": FAKE_PROVIDER_CODE,
        "model": "fake-model",
        "temperature": 0.7,
        "max_tokens": 512,
    }


# ============================================================================
# Scripted response logic
# ============================================================================

def _extract_node_name(prompt: str) -> str:
    """Extract the node name from the current-node info in NLU/NLG prompts."""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("节点名称:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_query(prompt: str) -> str:
    """Extract the first line after the user-input section marker as the user query."""
    marker = "### 用户输入"
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _route_nlu(query: str, retry: bool) -> str:
    """Scripted intent classification result for the route root node."""
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # triggers the first parse failure
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if any(k in query for k in ("买车", "购车", "试驾", "看车", "询价", "车型")):
        return '{"next_node": "menu_sales", "slots": {}}'
    return '{"next_node": "", "slots": {}}'  # unknown-intent fallback


def _fsm_nlu(node_name: str, query: str) -> str:
    """Scripted intent/slot extraction result for FSM nodes."""
    # Off-topic input -> clarify intent (fixed topic/keywords slots)
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )
    mapping = {
        "询问品牌": {"next_node": "buy_ask_budget", "slots": {"brand": query}},
        "询问预算": {"next_node": "buy_ask_city", "slots": {"budget": query}},
        "询问城市": {"next_node": "buy_confirm", "slots": {"city": query}},
        "确认购车信息": {"next_node": "", "slots": {}},
    }
    result = mapping.get(node_name, {"next_node": "", "slots": {}})
    return json.dumps(result, ensure_ascii=False)


def _unified(node_name: str, query: str, retry: bool) -> str:
    """Scripted result for the unified stage (single call + structured output).

    Output protocol: {"reply", "next_node", "slots"}; the reply embeds the
    target node name for easier assertions.
    """
    if "解析失败重试" in query and not retry:
        return "这不是合法的 JSON 输出"  # triggers the first parse failure
    if "永远解析失败" in query:
        return "这不是合法的 JSON 输出"
    if "跳到不存在节点" in query:
        # Simulates the model violating a transition-edge constraint, for the
        # code-level hard-guard test
        return json.dumps(
            {
                "reply": "统一回复: 非法节点",
                "next_node": "not_exist_node",
                "slots": {},
            },
            ensure_ascii=False,
        )
    if "硬造澄清意图" in query:
        # Simulates a module without clarify enabled emitting a clarify signal,
        # for the allowed-set hard-guard test
        return json.dumps(
            {
                "reply": "统一回复: 硬造澄清",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["硬造"]},
            },
            ensure_ascii=False,
        )

    # Off-topic input -> clarify intent (fixed topic/keywords slots, short
    # acknowledgment reply)
    if any(k in query for k in ("收别的钱", "其他收费", "额外收费")):
        return json.dumps(
            {
                "reply": "统一承接: 这个问题我帮您确认一下",
                "next_node": "clarify",
                "slots": {"topic": "费用", "keywords": ["额外收费"]},
            },
            ensure_ascii=False,
        )

    # Route root node: classify intent to a menu
    if node_name == "统一路由根节点":
        if any(k in query for k in ("买车", "购车", "车型", "试驾", "询价")):
            return json.dumps(
                {
                    "reply": "统一回复: 购车菜单",
                    "next_node": "u_menu_sales",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        if any(k in query for k in ("你好", "谢谢", "再见")):
            return json.dumps(
                {
                    "reply": "统一回复: 闲聊菜单",
                    "next_node": "u_menu_chitchat",
                    "slots": {},
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"reply": "统一回复: 统一路由根节点", "next_node": "", "slots": {}},
            ensure_ascii=False,
        )

    # FSM nodes: advance the flow + extract slots; the reply is the chosen
    # next node's script
    mapping = {
        "询问品牌": ("u_ask_budget", "brand", "统一回复: 询问预算"),
        "询问预算": ("u_confirm", "budget", "统一回复: 确认购车信息"),
        "确认购车信息": ("", None, "统一回复: 确认购车信息"),
    }
    next_node, slot_key, reply = mapping.get(
        node_name, ("", None, "统一回复: 未知节点")
    )
    slots = {slot_key: query} if slot_key else {}
    return json.dumps(
        {"reply": reply, "next_node": next_node, "slots": slots},
        ensure_ascii=False,
    )


def _extract_xianyu_section(prompt: str, marker: str) -> str:
    """Extract the first line of the given section (heading starting with ###) from a Xianyu NLG prompt."""
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    segment = prompt[idx + len(marker):]
    lines = [l.strip() for l in segment.split("\n") if l.strip()]
    return lines[0] if lines else ""


def _xianyu_nlg(prompt: str) -> str:
    """Xianyu intent NLG prompt (XIANYU_*_NLG_PROMPT, contains the buyer-message section).

    The reply carries the intent persona keyword from the task description,
    so assertions can tell which menu-node template was hit.
    """
    task = ""
    for line in prompt.split("\n"):
        line = line.strip()
        if line.startswith("## 任务描述"):
            idx = prompt.find(line)
            after = prompt[idx + len(line):].lstrip("\n").split("\n", 1)
            task = after[0].strip() if after else ""
            break
    if "议价" in task:
        return "闲鱼回复: 议价"
    if "技术" in task:
        return "闲鱼回复: 技术"
    return "闲鱼回复: 通用"


def scripted_response(prompt: str) -> str:
    """Return the scripted LLM output for the prompt type."""
    node_name = _extract_node_name(prompt)

    # Xianyu intent NLG prompt: no node-name line (uses node.base_nlg_prompt
    # intent templates); identified by the buyer-message section header
    # (checked before the generic NLG fallback)
    if "### 买家消息" in prompt:
        return _xianyu_nlg(prompt)

    # NLU retry/repair prompt (contains the repair-requirements section)
    # -> return the correct format per protocol
    if "修正要求" in prompt:
        query = _extract_query(prompt).replace("解析失败重试", "")
        if '"reply"' in prompt:
            return _unified(node_name, query, retry=True)
        return _route_nlu(query, retry=True)

    # Unified-stage prompt: three-field JSON protocol with reply + next_node
    # (checked before the NLU branch)
    if '"reply"' in prompt and '"next_node"' in prompt:
        return _unified(node_name, _extract_query(prompt), retry=False)

    # NLU prompt: requires next_node JSON output
    if '"next_node"' in prompt:
        query = _extract_query(prompt)
        if node_name == "路由根节点":
            return _route_nlu(query, retry=False)
        return _fsm_nlu(node_name, query)

    # Clarify prompt (contains the KB recall-content section and is not an
    # NLU JSON protocol) -> return per mode
    if "知识库召回内容" in prompt and '"next_node"' not in prompt:
        if "召回内容为空或无相关内容" in prompt or "（无相关知识库内容）" in prompt:
            return "承接：该问题暂无法详细解答。请问您的预算大概是多少呢？"
        return "解答：除车价外仅收取上牌费与服务费。请问您的预算大概是多少呢？"

    # NLG prompt: the reply text carries the current node name, so assertions
    # can tell which node NLG used
    if node_name:
        return f"回复: {node_name}"
    return "回复: 无当前节点"
