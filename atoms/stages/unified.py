"""
Unified stage — single call + structured output: one LLM call produces both
intent/slots (NLU) and the reply text (NLG).

Differences from the default two-stage NLU → NLG:
- Only one serial LLM call per turn (latency/cost roughly halved);
- The decision (next_node/slots) and the text (reply) come from the same inference,
  naturally self-consistent;
- next_node is hard-validated by code against the current node's legal transition
  edges; illegal values fall back to staying on the current node (deterministic
  guard, not reliant on prompt constraints).

Output protocol (JSON returned by the single call):
    {"reply": "reply text for the user", "next_node": "xx", "slots": {"slot1": ""}}

Stage output split-writes:
- ctx.nlu_result = {"next_node", "slots"}      — downstream node jumps unchanged
- ctx.nlg_result = {"content": reply}          — downstream reply extraction unchanged
- ctx.metadata["unified"] = observability info (invalid_next_node / parse_failed etc.)

Wiring (plan-② declarative form — string codes in module/node ``stages``,
resolved from the plugin registry at execution time):
    FSMModule(stages={"nlu": "fsm_unified", "nlg": "nlg_pass_through"})
    RouteModule(stages={"nlu": "route_unified", "nlg": "nlg_pass_through"})
(node-level stages take priority over module-level, see pipeline.py; the unified
stage writes both nlu_result and nlg_result — nlu/nlg may also share the unified
code directly, the runner dedups to a single execution)

Combined with dual-track clarify (FSM modules declaring the clarify slot):
    The pipeline assembles as [unified stage, ClarifyStage, PassThroughNLG].
    The unified stage outputs next_node="clarify" per the off-topic special case in
    the template (admitted by the valid set); ClarifyStage overwrites nlg_result to
    generate the clarify reply; PassThroughNLG lets it through;
    a clarify turn costs 2 LLM calls (unified + clarify generation), on par with the
    two-stage + clarify setup; normal turns stay at 1.

Under ROUTE modules the unified stage's reply already follows the chosen menu node's
answer style; the subsequent route_advance / jump_module dispatch in the pipeline is
unaffected.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from nexus.context import (
    DialogueContext,
    PipelineStage,
    fill_prompt_template,
)
from atoms.stages.nlu.nlu import BaseNLU
from atoms.stages._prompts import (
    FSM_UNIFIED_DEFAULT_PROMPT,
    ROUTE_UNIFIED_DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)


class _UnifiedBaseNLU(BaseNLU):
    """Unified stage base class: one call completes understanding and generation, split-writing nlu_result / nlg_result.

    Subclasses only supply the default template and log wording; parse tolerance and
    failure retry reuse ``BaseNLU._execute_with_retry`` (the retry prompt is overridden
    by this class to the three-field protocol).
    """

    # Fallback reply after parse retries are exhausted: guarantees a reply this turn
    # and staying on the current node
    fallback_reply = "抱歉，我没能理解您的意思，请您换个说法再告诉我一次。"

    def __init__(
        self,
        response_format: Optional[Dict[str, Any]] = None,
        fallback_reply: Optional[str] = None,
    ):
        """
        Args:
            response_format: optional API-level structured constraint (e.g.
                ``{"type": "json_object"}``), passed through into the request payload
                via provider ``**kwargs``. Default None — the output format is
                constrained only by the prompt protocol, portable across providers;
                recommended when the server side supports it.
            fallback_reply: fallback reply after parse retries are exhausted; defaults
                to the class-attribute text.
        """
        self.response_format = response_format
        if fallback_reply is not None:
            self.fallback_reply = fallback_reply

    # ------------------------------------------------------------------
    # LLM call: passes response_format through on top of BaseNLU as needed
    # ------------------------------------------------------------------

    async def _call_llm(
        self, prompt: str, llm_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """Call the LLM (single call per turn) and return the raw response text.

        When the provider streams natively AND a turn emitter is attached
        (chat_turn_stream path), the call runs streamed: the ``reply``
        field's value is forwarded to the consumer incrementally as it
        arrives (ReplyFieldTap — the other JSON fields never leak), while
        the chunks still aggregate into the full raw text for parsing.
        Non-streaming providers / plain chat_turn turns take the legacy
        one-shot path unchanged.
        """
        if llm_config is None:
            from nexus.settings import get_llm_config
            llm_config = get_llm_config()

        from nexus.llm.resolve import build_provider
        from nexus.engine.streaming import current_emitter, stream_llm_reply

        provider = build_provider(llm_config)
        extra_kwargs: Dict[str, Any] = {}
        if self.response_format is not None:
            extra_kwargs["response_format"] = self.response_format

        messages = [{"role": "user", "content": prompt}]
        if (hasattr(provider, "achat_completion_stream")
                and current_emitter.get() is not None):
            result = await stream_llm_reply(
                provider.achat_completion_stream(
                    messages=messages,
                    model=llm_config["model"],
                    temperature=llm_config.get("temperature", 0.7),
                    max_tokens=llm_config.get("max_tokens", 2048),
                    **extra_kwargs,
                ),
                field="reply",
            )
        else:
            result = await provider.achat_completion(
                messages=messages,
                model=llm_config["model"],
                temperature=llm_config.get("temperature", 0.7),
                max_tokens=llm_config.get("max_tokens", 2048),
                **extra_kwargs,
            )

        content = result.get("content", "")
        logger.debug("Unified LLM 返回: %s", content[:200])
        return content

    # ------------------------------------------------------------------
    # Candidate nodes and legal transition edges
    # ------------------------------------------------------------------

    def _candidate_node_codes(self, cxt: DialogueContext) -> List[str]:
        """Legal transition targets of the current node (sub_nodes); empty when there is no current node."""
        node = cxt.get_current_node()
        return list(node.sub_nodes) if node is not None else []

    def _valid_next_values(self, cxt: DialogueContext) -> set:
        """Legal values for next_node: candidate node codes + empty string (stay on current node).

        When the module declares the clarify slot (a non-None ``clarify`` in
        module.stages — the plan-② replacement of enable_clarify), "clarify" is
        additionally admitted: once triggered, ClarifyStage overwrites nlg_result to
        generate the clarify reply, and the node transition guard skips via
        metadata["clarify"], so it never actually jumps to a nonexistent node.
        """
        valid = set(self._candidate_node_codes(cxt)) | {""}
        module = cxt.get_current_module()
        if module is not None and (getattr(module, "stages", None) or {}).get("clarify"):
            valid.add("clarify")
        return valid

    def _format_valid_values(self, cxt: DialogueContext) -> str:
        """Prompt text of the legal value list (embedded in the template's next_node valid-values section)."""
        return json.dumps(sorted(self._valid_next_values(cxt)), ensure_ascii=False)

    # ------------------------------------------------------------------
    # Candidate nodes' answer styles — the unified stage needs the destination's
    # text style to generate in a single pass
    # ------------------------------------------------------------------

    def _format_next_node_pattern(self, cxt: DialogueContext) -> str:
        """Prompt text of the candidate next nodes (with answer styles).

        Compared with the two-stage NLU's next_node slot (code + name + description
        only), this additionally carries each candidate node's slot definitions and
        answer styles: the model picks the node and writes the reply within the same
        inference, so the reply style is the chosen node's answer style.
        """
        parts: List[str] = []
        for code in self._candidate_node_codes(cxt):
            sub_node = cxt.node_map.get(code)
            if sub_node is None:
                parts.append(f"- 节点编码: {code}")
                continue

            seg = [f"- 节点编码: {code}"]
            if sub_node.node_name:
                seg.append(f"  节点名称: {sub_node.node_name}")
            if sub_node.node_description:
                seg.append(f"  节点描述: {sub_node.node_description}")
            if sub_node.node_slots:
                seg.append(
                    "  槽位定义: "
                    + json.dumps(sub_node.node_slots, ensure_ascii=False)
                )
            if sub_node.answer_examples:
                for example in sub_node.answer_examples:
                    seg.append(f"  回答范式: {example}")
            parts.append("\n".join(seg))

        if not parts:
            return "暂无候选后续节点（当前为终节点，next_node 输出空字符串）"
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Prompt assembly (reuses BaseNLU template priority node > module > default)
    # ------------------------------------------------------------------

    def _build_template_kwargs(self, cxt: DialogueContext) -> Dict[str, str]:
        """The unified stage's slot vocabulary: the NLU vocabulary plus answer styles and legal values."""
        return {
            "cur_node": cxt.format_cur_node(stage="nlu"),
            "cur_answer_pattern": cxt.format_answer_pattern(),
            "next_node_pattern": self._format_next_node_pattern(cxt),
            "valid_next_values": self._format_valid_values(cxt),
            "query": cxt.user_query,
            "query_rewrite": cxt.format_rewritten_queries(),
            "recall_info": cxt.format_recall_info(),
            "filled_slots": cxt.format_slots(),
            "history": cxt.format_history(),
            "task_info": cxt.format_task_info(),
        }

    def _build_retry_prompt(self, original_prompt: str, failed_output: str) -> str:
        """Parse-failure retry prompt: corrects the output to the reply/next_node/slots three-field protocol."""
        return (
            "## 原始任务\n"
            f"{original_prompt}\n\n"
            "## 上一次输出（格式不符合 JSON 规范，请修正）\n"
            f"{failed_output}\n\n"
            "## 修正要求\n"
            "请严格按照以下 JSON 格式重新输出，不要包含任何额外内容：\n\n"
            '{"reply": "给用户的回复话术", "next_node": "xx", "slots": {...}}\n\n'
            "注意：\n"
            "1. reply 是面向用户的自然语言回复\n"
            "2. next_node 必须在候选后续节点中存在（无法推进时输出空字符串）\n"
            "3. slots 按照给定节点的 slots 模版进行抽取\n"
            "4. 只输出 JSON 对象，不要包裹 markdown 代码块或其他文字"
        )

    # ------------------------------------------------------------------
    # Single-call main logic: parse → hard validation → split-write nlu_result / nlg_result
    # ------------------------------------------------------------------

    async def _execute_unified(self, ctx: DialogueContext) -> None:
        """One call and split-write of the outputs; any failure degrades to the fallback reply, never raising upward."""
        prompt = self.prompt_build(ctx)
        parsed = await self._execute_with_retry(prompt, ctx.llm_config)

        unified_meta: Dict[str, Any] = {"triggered": True}

        if "raw" in parsed:
            # Parse retries exhausted: stay on the current node + fallback reply
            logger.warning(
                "统一阶段解析失败（含重试），使用兜底回复: session=%s",
                ctx.session_id,
            )
            unified_meta["parse_failed"] = True
            ctx.nlu_result = {"next_node": "", "slots": {}}
            ctx.nlg_result = {"content": self.fallback_reply, "fallback": True}
        else:
            next_node = str(parsed.get("next_node", "") or "").strip()
            valid_values = self._valid_next_values(ctx)

            if next_node not in valid_values:
                # Hard guard: illegal transition edge → stay on the current node.
                # If the rejected value is a clarify signal (module without dual-track
                # clarify enabled), the model's reply is usually a "let me confirm for
                # you" take-over promise that no later clarify step will honor —
                # replace the reply with the fallback as well.
                logger.warning(
                    "统一阶段 next_node '%s' 不在合法转移边 %s 中，保持当前节点: %s",
                    next_node,
                    sorted(valid_values),
                    ctx.current_node_code,
                )
                unified_meta["invalid_next_node"] = next_node
                next_node = ""

            reply = str(parsed.get("reply", "") or "").strip() or self.fallback_reply
            if unified_meta.get("invalid_next_node") == "clarify":
                reply = self.fallback_reply
            ctx.nlu_result = {
                "next_node": next_node,
                "slots": parsed.get("slots", {}) or {},
            }
            ctx.nlg_result = {"content": reply}

        unified_meta["reply"] = ctx.nlg_result["content"]
        ctx.metadata["unified"] = unified_meta


# ============================================================================
# FSM module unified stage
# ============================================================================

class FSMUnifiedNLU(_UnifiedBaseNLU):
    """FSM module unified stage: one structured call completes intent/slot extraction and reply generation.

    Paired with ``PassThroughNLG`` (the nlg position of the generate dict), the FSM
    pipeline goes from [NLU, NLG] two LLM calls to a single unified-stage call;
    node transition and slot merge logic (_handle_node_transition) unchanged.
    """

    stage_name = "fsm_unified"

    def _default_prompt_template(self) -> str:
        return FSM_UNIFIED_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        return self._fill_template(prompt_template, kwargs)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        await self._execute_unified(ctx)
        logger.info(
            "FSM 统一阶段完成: session=%s, next_node=%s, reply_len=%d",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
            len(ctx.nlg_result.get("content", "")),
        )
        return ctx


# ============================================================================
# ROUTE module unified stage
# ============================================================================

class RouteUnifiedNLU(_UnifiedBaseNLU):
    """ROUTE module unified stage: one structured call completes intent classification and menu-node reply generation.

    Candidates are the routing root's menu nodes (each with its answer style); the
    pipeline's subsequent route_advance switches the current node to the chosen menu,
    and jump_module dispatch is unaffected.
    """

    stage_name = "route_unified"

    def _default_prompt_template(self) -> str:
        return ROUTE_UNIFIED_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)
        return self._fill_template(prompt_template, kwargs)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        await self._execute_unified(ctx)
        logger.info(
            "Route 统一阶段完成: session=%s, next_node=%s, reply_len=%d",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
            len(ctx.nlg_result.get("content", "")),
        )
        return ctx


# ============================================================================
# Placeholder NLG — paired with the unified stage
# ============================================================================

class PassThroughNLG(PipelineStage):
    """Placeholder NLG stage: preserves the nlg_result already written by the unified stage, skipping a second generation.

    Usage (plan-② declarative form): ``module.stages = {"nlu": "fsm_unified"/"route_unified",
    "nlg": PassThroughNLG()}`` (or the unified stage directly as the generate
    single-stage form, where the nlg component guard auto no-ops), replacing the
    default NLG's second LLM call.

    When earlier stages such as clarify turns overwrite nlg_result, it is likewise
    passed through; if no stage earlier in the pipeline generated nlg_result (wiring
    error), this turn's reply is empty and a warning is logged.
    """

    stage_name = "nlg_pass_through"

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        if ctx.nlg_result is None:
            logger.warning(
                "PassThroughNLG 未检测到已生成的 nlg_result，"
                "请确认统一阶段已配置为 module 的 generate（单 stage 形态）"
                "或 pattern.stages 显式装配: session=%s",
                ctx.session_id,
            )
            ctx.nlg_result = {"content": ""}
        else:
            logger.debug(
                "PassThroughNLG 跳过生成（沿用已写入回复）: session=%s",
                ctx.session_id,
            )
        return ctx


# ============================================================================
# Opening broadcast — zero-LLM pure assembly stage
# ============================================================================

class OpeningBroadcastNLG(PipelineStage):
    """Opening broadcast stage: assembles task_info fields with a text template into a greeting, written directly
    to nlg_result["content"] (zero LLM calls).

    Wiring: mounted on the entry node's ``generate`` (single-stage form). Once the FSM
    transitions away it never returns to that node, so it naturally broadcasts only
    once; nlu_result stays empty → the transition guard stays on the current node
    until a business node takes over.

    Two template levels:
    - ``template`` (constructor arg): str.format embeds task_info fields field by
      field, e.g. ``"Hello, I am the intelligent assistant for {product_name}"``;
    - Not provided / format failure (missing fields etc.): falls back to the default
      text + task_info key-value pairs joined line by line (same source and format
      as ctx.format_task_info).
    """

    stage_name = "opening_broadcast"

    DEFAULT_TEMPLATE = "您好，很高兴为您服务！"

    def __init__(self, template: Optional[str] = None):
        """
        Args:
            template: greeting text template; ``{field}`` placeholders map to
                task_info fields; when omitted, falls back to the class default text
                + task_info key-value assembly.
        """
        self.template = template

    def _task_info(self, ctx: DialogueContext) -> Dict[str, Any]:
        """Same source as ctx.format_task_info: task_basic_info first, metadata as fallback."""
        return ctx.task_basic_info or ctx.metadata.get("task_info") or {}

    def _build_content(self, ctx: DialogueContext) -> str:
        """Assemble the greeting: template.format fills fields in; on failure falls back to default assembly."""
        task_info = self._task_info(ctx)

        if self.template:
            try:
                return self.template.format(**task_info)
            except (KeyError, IndexError, ValueError) as e:
                logger.warning(
                    "开场白模板格式化失败（task_info 缺字段或格式非法），"
                    "回落默认拼接: template=%r, error=%s",
                    self.template, e,
                )

        parts = [self.DEFAULT_TEMPLATE]
        parts.extend(f"{key}: {value}" for key, value in task_info.items())
        return "\n".join(parts)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        content = self._build_content(ctx)
        ctx.nlg_result = {"content": content}
        logger.info(
            "开场白播报完成: session=%s, content_len=%d",
            ctx.session_id,
            len(content),
        )
        return ctx
