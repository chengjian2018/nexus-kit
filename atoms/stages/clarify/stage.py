"""ClarifyStage — integrated dual-track clarify stage (discrimination + retrieval + gating + generation).

Insertion point: the clarify slot in the pipeline skeleton (only modules declaring
the clarify slot in stages get it resolved in).

Execution flow (see spec 5.1 for details):
1. Reset ctx.metadata["clarify"] = {"triggered": False} each turn
2. Trigger check: nlu_result.next_node == "clarify"
3. Take the fixed clarify slots topic / keywords
4. Assemble the retrieval query: user_query + topic + keywords
5. Knowledge base recall (dedicated MultiPathRecaller)
6. ClarifyRouteRule gating -> one of three modes
7. Generate the reply from the template selected by mode (the only NLG call this turn), write ctx.nlg_result
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from atoms.stages.clarify.prompts import CLARIFY_PROMPTS
from atoms.stages.clarify.rule import ClarifyRouteRule
from nexus.context import DialogueContext, PipelineStage, fill_prompt_template
from atoms.stages.recaller import MultiPathRecaller

logger = logging.getLogger(__name__)

CLARIFY_NODE_CODE = "clarify"


class ClarifyStage(PipelineStage):
    """Dual-track clarify stage."""

    stage_name = "clarify_stage"

    def __init__(
        self,
        recaller: MultiPathRecaller,
        rule: Optional[ClarifyRouteRule] = None,
        default_prompt: Optional[str] = None,
    ):
        self.recaller = recaller
        self.rule = rule or ClarifyRouteRule()
        self.default_prompt = default_prompt

    # ------------------------------------------------------------------
    # LLM generation (same call chain as NLU/NLG; can be wholly replaced in tests)
    # ------------------------------------------------------------------

    def _generate(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
        """Call the LLM to generate the clarify reply."""
        if llm_config is None:
            from nexus.settings import get_llm_config
            llm_config = get_llm_config()

        from nexus.llm.resolve import build_provider
        provider = build_provider(llm_config)
        messages = [{"role": "user", "content": prompt}]
        result = provider.chat_completion(
            messages=messages,
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
        )
        return result.get("content", "")

    @staticmethod
    def _is_triggered(ctx: DialogueContext) -> bool:
        """Trigger check: next_node == clarify.

        When the NLU-emitted next_node is not in node_map (invalid code, and the
        injected topology is non-empty), fall back to clarify — the clarify
        fallback takes over instead of silently keeping the current node.
        """
        nlu_result = ctx.nlu_result or {}
        next_node = nlu_result.get("next_node", "")
        if (ctx.node_map and next_node
                and next_node not in ctx.node_map):
            logger.warning(
                "NLU next_node '%s' 不在 node_map，回落 clarify 兜底",
                next_node,
            )
            nlu_result["next_node"] = CLARIFY_NODE_CODE
        return nlu_result.get("next_node") == CLARIFY_NODE_CODE

    @staticmethod
    def _extract_open_slots(ctx: DialogueContext) -> Dict[str, Any]:
        slots = (ctx.nlu_result or {}).get("slots", {}) or {}
        topic = slots.get("topic", "") or ""
        keywords = slots.get("keywords", []) or []
        if isinstance(keywords, str):
            keywords = [keywords]
        return {"topic": topic, "keywords": [str(k) for k in keywords]}

    @staticmethod
    def _build_search_query(ctx: DialogueContext, open_slots: Dict[str, Any]) -> str:
        parts = [ctx.user_query, open_slots["topic"], *open_slots["keywords"]]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _format_recall_for_prompt(results: List[Dict[str, Any]], top_n: int = 3) -> str:
        if not results:
            return "（无相关知识库内容）"
        lines = []
        for r in results[:top_n]:
            lines.append(f"- {r.get('id', '')}: {r.get('content', '')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Recall execution (exceptions degrade to an empty result; gating then
    # naturally falls back)
    # ------------------------------------------------------------------

    def _do_recall(self, ctx: DialogueContext, search_query: str) -> List[Dict[str, Any]]:
        """Run recall with the assembled query and return fused results; return an empty list on error.

        MultiPathRecaller.execute uses ctx.user_query as the search text and writes
        ctx.pre_recall_results — temporarily swap user_query here and restore it
        after execution, so the original question seen by downstream stages
        stays unpolluted.
        """
        original_query = ctx.user_query
        original_recall = ctx.pre_recall_results
        try:
            ctx.user_query = search_query
            self.recaller.phase = "pre"
            self.recaller.execute(ctx)
            return list(ctx.pre_recall_results)
        except Exception as e:
            logger.warning("澄清检索异常，降级 fallback: %s", e, exc_info=True)
            return []
        finally:
            ctx.user_query = original_query
            ctx.pre_recall_results = original_recall

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        # 1. Per-turn reset (prevent cross-turn residue)
        ctx.metadata["clarify"] = {"triggered": False}

        # 2. Trigger check (the module switch is guaranteed by the pipeline
        # assembly side; the stage only looks at intent)
        if not self._is_triggered(ctx):
            return ctx

        open_slots = self._extract_open_slots(ctx)
        search_query = self._build_search_query(ctx, open_slots)

        # 3-6. Recall + gating (exceptions degrade to fallback, never blocking
        # the main pipeline)
        recall_results = self._do_recall(ctx, search_query)
        try:
            mode, adjusted = self.rule.route(
                recall_results, open_slots["topic"], open_slots["keywords"]
            )
        except Exception as e:
            logger.warning("澄清门控异常，降级 fallback: %s", e, exc_info=True)
            mode, adjusted = "fallback", recall_results
        logger.info(
            "澄清门控: session=%s, mode=%s, top_score=%s, query=%r",
            ctx.session_id, mode,
            adjusted[0].get("score") if adjusted else None,
            search_query,
        )

        # 7. Generate by mode (the only NLG call this turn)
        prompt = self._build_prompt(ctx, mode, open_slots, adjusted)
        try:
            content = self._generate(prompt, ctx.llm_config).strip()
        except Exception as e:
            logger.warning("澄清生成异常，使用兜底话术: %s", e, exc_info=True)
            content = "抱歉，这个问题我需要确认一下。我们继续刚才的任务好吗？"

        ctx.nlg_result = {"content": content}
        ctx.metadata["clarify"] = {
            "triggered": True,
            "mode": mode,
            "recall_results": adjusted,
            "open_slots": open_slots,
            "query": search_query,
        }
        return ctx

    def _build_prompt(
        self,
        ctx: DialogueContext,
        mode: str,
        open_slots: Dict[str, Any],
        recall_results: List[Dict[str, Any]],
    ) -> str:
        template = CLARIFY_PROMPTS.get(
            mode, self.default_prompt or CLARIFY_PROMPTS["fallback"]
        )
        keywords_text = "、".join(open_slots["keywords"]) or "（无）"
        slots = {
            "query": ctx.user_query,
            "topic": open_slots["topic"] or "（无）",
            "keywords": keywords_text,
            "recall_info": self._format_recall_for_prompt(recall_results),
            "cur_node": ctx.format_cur_node(stage="nlg"),
            "history": ctx.format_history(),
            "task_info": ctx.format_task_info(),
        }
        return fill_prompt_template(template, slots)
