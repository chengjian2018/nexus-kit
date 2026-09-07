"""
NLU (Natural Language Understanding) stage — intent recognition and slot extraction.

Supports NLU processing for both FSM and ROUTE module types:
- FSMNLU  : intent recognition and transitions within finite state machine modules
- RouteNLU: intent classification and dispatch for top-level route modules

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
from atoms.stages._prompts import FSM_NLU_DEFAULT_PROMPT, ROUTE_NLU_DEFAULT_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# NLU stage base class
# ============================================================================

class BaseNLU(PipelineStage, ABC):
    """NLU stage base class, integrated into the Pipeline system.

    Subclasses must implement ``_default_prompt_template``, ``prompt_build`` and ``execute``.
    """

    stage_name = "nlu"

    # ------------------------------------------------------------------
    # LLM client
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
        """Call the LLM and return the response text.

        When *llm_config* is None, the config is auto-loaded from
        ``config/local_config.yaml`` and the model is taken from the loaded
        config (not from the caller's argument).
        """
        if llm_config is None:
            from nexus.settings import get_llm_config
            llm_config = get_llm_config()

        provider = build_provider(llm_config)

        messages = [{"role": "user", "content": prompt}]
        result = provider.chat_completion(
            messages=messages,
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
        )

        content = result.get("content", "")
        logger.debug("NLU LLM 返回: %s", content[:200])
        return content

    # ------------------------------------------------------------------
    # Result parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nlu_result(raw: str) -> Dict[str, Any]:
        """Parse the NLU result from the raw LLM response.

        Expected format (per the prompt template definition):
            {"next_node": "xx", "slots": {"slot1": "", "slot2": []}}

        Fault tolerance:
        - strip markdown code block markers
        - try to extract the first JSON object

        Returns:
            ``{"next_node": ..., "slots": ...}`` on success; on failure
            ``{"next_node": "", "slots": {}, "raw": raw}`` — the presence of
            the ``"raw"`` key indicates a parse failure.
        """
        raw = raw.strip()

        # Strip markdown code block wrapper
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        # Try direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Try to extract the first JSON object {...}
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("无法解析 NLU 结果: %s", raw[:200])
        return {"next_node": "", "slots": {}, "raw": raw}

    # ------------------------------------------------------------------
    # Retry mechanism
    # ------------------------------------------------------------------

    @staticmethod
    def _build_retry_prompt(original_prompt: str, failed_output: str) -> str:
        """Build a retry prompt, using the last failed output as correction context.

        Args:
            original_prompt: full prompt of the first request.
            failed_output: raw text returned by the first LLM call (the content that failed to parse).
        """
        return (
            "## 原始任务\n"
            f"{original_prompt}\n\n"
            "## 上一次输出（格式不符合 JSON 规范，请修正）\n"
            f"{failed_output}\n\n"
            "## 修正要求\n"
            "请严格按照以下 JSON 格式重新输出，不要包含任何额外内容：\n\n"
            '{"next_node": "xx", "slots": {...}}\n\n'
            "注意：\n"
            "1. next_node 必须在后续节点中存在\n"
            "2. slots 按照给定节点的 slots 模版进行抽取\n"
            "3. 只输出 JSON 对象，不要包裹 markdown 代码块或其他文字"
        )

    def _execute_with_retry(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None, max_retries: int = 1) -> Dict[str, Any]:
        """Run the NLU call, auto-retrying once when parsing fails.

        On retry, the last failed output is fed back as supplementary input to guide the LLM in fixing the format.

        Args:
            prompt: full prompt of the first request.
            llm_config: LLM config, passed from ctx.llm_config.
            max_retries: max retry count, default 1.

        Returns:
            parsed NLU result dict.
        """
        raw = self._call_llm(prompt, llm_config)
        result = self._parse_nlu_result(raw)

        # Parsed successfully (no "raw" fallback key), return directly
        if "raw" not in result:
            return result

        # Parse failed, enter retry
        for attempt in range(1, max_retries + 1):
            logger.warning(
                "NLU 结果解析失败，第 %d/%d 次重试...",
                attempt, max_retries,
            )
            retry_prompt = self._build_retry_prompt(prompt, raw)
            raw = self._call_llm(retry_prompt, llm_config)
            result = self._parse_nlu_result(raw)

            if "raw" not in result:
                logger.info("NLU 重试成功（第 %d 次）", attempt)
                return result

        logger.warning("NLU 重试 %d 次后仍失败，返回兜底结果", max_retries)
        return result

    # ------------------------------------------------------------------
    # Prompt template selection (priority: node > module > default)
    # ------------------------------------------------------------------

    def _resolve_prompt_template(self, cxt: DialogueContext) -> str:
        """Resolve the prompt template by priority: node > module > class default."""
        system_prompt = resolve_prompt_template(
            cxt, "base_nlu_prompt", self._default_prompt_template()
        )
        if system_prompt is None:
            return self._default_prompt_template()
        return system_prompt


    def _default_prompt_template(self) -> str:
        """Subclasses may override this method to return the default template."""
        return FSM_NLU_DEFAULT_PROMPT

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
            "cur_node": cxt.format_cur_node(stage="nlu"),
            "next_node": cxt.format_next_nodes(),
            "jump_modules": cxt.format_jump_modules(),
            "query": cxt.user_query,
            "query_rewrite": cxt.format_rewritten_queries(),
            "recall_info": cxt.format_recall_info(),
            "history": cxt.format_history(),
            "filled_slots": cxt.format_slots(),
        }

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    def prompt_build(self, cxt: DialogueContext) -> str:
        """Build the NLU prompt and return the formatted string."""
        ...

    @abstractmethod
    def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run the NLU stage and write the result into ctx.nlu_result."""
        ...


# ============================================================================
# FSM module NLU
# ============================================================================

class FSMNLU(BaseNLU):
    """NLU implementation for FSM modules — intent recognition and slot extraction within the state machine.

    Uses ``FSM_NLU_DEFAULT_PROMPT`` as the default template; node/module level override supported.
    """

    stage_name = "fsm_nlu"

    def _default_prompt_template(self) -> str:
        return FSM_NLU_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)

        return self._fill_template(prompt_template, kwargs)

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        prompt = self.prompt_build(ctx)
        nlu_result = self._execute_with_retry(prompt, ctx.llm_config)
        ctx.nlu_result = nlu_result
        logger.info(
            "FSM NLU 完成: session=%s, next_node=%s",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
        )
        return ctx


# ============================================================================
# Route module NLU
# ============================================================================

class RouteNLU(BaseNLU):
    """NLU implementation for Route modules — top-level route intent classification and dispatch.

    Uses ``ROUTE_NLU_DEFAULT_PROMPT`` as the default template; node/module level override supported.
    """

    stage_name = "route_nlu"

    def _default_prompt_template(self) -> str:
        return ROUTE_NLU_DEFAULT_PROMPT

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)

        return self._fill_template(prompt_template, kwargs)

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        prompt = self.prompt_build(ctx)
        ctx.nlu_result = self._execute_with_retry(prompt, ctx.llm_config)
        logger.info(
            "Route NLU 完成: session=%s, next_node=%s",
            ctx.session_id,
            ctx.nlu_result.get("next_node", ""),
        )
        return ctx