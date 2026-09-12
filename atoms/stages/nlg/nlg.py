"""
NLG (Natural Language Generation) stage — reply wording generation.

Supports NLG processing for both FSM and ROUTE module types:
- FSMNLG  : reply generation within finite state machine modules
- RouteNLG: reply generation for top-level route modules

LLM call chain: config/local_config.yaml → build_provider → chat_completion.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from nexus.context import (
    DialogueContext,
    PipelineStage,
    fill_prompt_template,
    resolve_prompt_template,
)
from nexus.llm.resolve import build_provider
from atoms.stages._prompts import FSM_NLG_DEFAULT_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# NLG stage base class
# ============================================================================

class BaseNLG(PipelineStage, ABC):
    """NLG stage base class, integrated into the Pipeline system.

    Subclasses must implement ``_default_prompt_template``, ``prompt_build`` and ``execute``.
    """

    stage_name = "nlg"

    # ------------------------------------------------------------------
    # LLM client
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
        """Call the LLM and return the response text.

        When *llm_config* is None, the config is auto-loaded from
        ``config/local_config.yaml`` and the model is taken from the loaded
        config (not from the caller's argument).

        When the provider streams natively AND a turn emitter is attached
        (chat_turn_stream path), the call runs streamed: the generated
        wording is user-visible text, so every chunk forwards as a delta
        while still aggregating into the full result. Non-streaming
        providers / plain chat_turn turns take the legacy path unchanged.
        """
        if llm_config is None:
            from nexus.settings import get_llm_config
            llm_config = get_llm_config()

        provider = build_provider(llm_config)

        messages = [{"role": "user", "content": prompt}]
        kwargs: Dict[str, Any] = dict(
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
        )
        from nexus.engine.streaming import current_emitter, stream_llm_reply
        if (hasattr(provider, "achat_completion_stream")
                and current_emitter.get() is not None):
            result = await stream_llm_reply(
                provider.achat_completion_stream(messages=messages, **kwargs))
        else:
            result = await provider.achat_completion(messages=messages, **kwargs)

        content = result.get("content", "")
        logger.debug("NLG LLM 返回: %s", content[:200])
        return content

    # ------------------------------------------------------------------
    # Prompt template selection (priority: node > module > default)
    # ------------------------------------------------------------------

    def _resolve_prompt_template(self, cxt: DialogueContext) -> str:
        """Resolve the prompt template by priority: node > module > class default."""
        system_prompt = resolve_prompt_template(
            cxt, "base_nlg_prompt", self._default_prompt_template()
        )
        if system_prompt is None:
            return self._default_prompt_template()
        return system_prompt

    def _default_prompt_template(self) -> str:
        """Subclasses may override this method to return the default template."""
        return FSM_NLG_DEFAULT_PROMPT

    # ------------------------------------------------------------------
    # Template filling — slot name → data-layer formatting mapping
    # (assembled in node.py / module.py / base.py; this layer only maps)
    # ------------------------------------------------------------------

    @staticmethod
    def _fill_template(template: str, kwargs: Dict[str, str]) -> str:
        """Replace ``{__key__}`` placeholders in the template with their values."""
        return fill_prompt_template(template, kwargs)

    def _build_template_kwargs(self, cxt: DialogueContext) -> Dict[str, str]:
        """Collect all template variables and return them together.

        Keys are fixed slot names (without the ``__`` prefix); values come from
        the data-layer formatting methods.
        """
        return {
            "cur_node": cxt.format_cur_node(stage="nlg"),
            "query": cxt.user_query,
            "query_rewrite": cxt.format_rewritten_queries(),
            "answer_pattern": cxt.format_answer_pattern(),
            "filled_slots": cxt.format_slots(),
            "history": cxt.format_history(),
            "task_info": cxt.format_task_info(),
        }

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    def prompt_build(self, cxt: DialogueContext) -> str:
        """Build the NLG prompt and return the formatted string."""
        ...

    @abstractmethod
    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run the NLG stage and write the result into ctx.nlg_result."""
        ...


# ============================================================================
# FSM module NLG
# ============================================================================

class FSMNLG(BaseNLG):
    """NLG implementation for FSM modules — reply generation within the state machine.

    Uses ``FSM_NLG_DEFAULT_PROMPT`` as the default template; node/module level override supported.
    """

    stage_name = "fsm_nlg"

    def _default_prompt_template(self) -> str:
        return FSM_NLG_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)

        return self._fill_template(prompt_template, kwargs)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        # Clarify turn: nlg_result was already written by ClarifyStage; skip to
        # avoid generating twice
        if (ctx.metadata.get("clarify") or {}).get("triggered"):
            logger.info("FSMNLG 跳过（澄清轮已生成）: session=%s", ctx.session_id)
            return ctx

        prompt = self.prompt_build(ctx)
        raw = await self._call_llm(prompt, ctx.llm_config)
        ctx.nlg_result = {"content": raw.strip()}
        logger.info(
            "FSM NLG 完成: session=%s, content_len=%d",
            ctx.session_id,
            len(raw),
        )
        return ctx


# ============================================================================
