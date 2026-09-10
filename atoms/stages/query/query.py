"""
Query Rewrite stage — rewrite and expand the user query.

Rewrite strategies include completing omitted info, resolving references,
synonym expansion, colloquial-to-formal conversion, etc. Combined with
dialogue history and context, it generates multiple retrieval-friendly query variants.

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
from atoms.stages._prompts import QUERY_REWRITE_DEFAULT_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# Query Rewrite stage base class
# ============================================================================

class BaseQueryRewriter(PipelineStage, ABC):
    """Query Rewrite stage base class, integrated into the Pipeline system.

    Subclasses must implement ``prompt_build`` and ``execute``; they may override
    ``_default_prompt_template`` to provide a custom default template.

    Node/module level prompt template override is supported via the ``base_query_rewrite_prompt`` attribute.
    """

    stage_name = "query_rewrite"

    # ------------------------------------------------------------------
    # LLM client
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
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
        result = await provider.achat_completion(
            messages=messages,
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2048),
        )

        content = result.get("content", "")
        logger.debug("QueryRewriter LLM 返回: %s", content[:200])
        return content

    # ------------------------------------------------------------------
    # Result parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_rewrite_result(raw: str) -> Dict[str, Any]:
        """Parse the query rewrite result from the raw LLM response.

        Expected format: {"rewritten_queries": ["rewrite1", ...], "reason": "..."}

        Fault tolerance: strip markdown code blocks, try to extract the JSON object.
        On parse failure the result is flagged via the ``"raw"`` key.
        """
        raw = raw.strip()

        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        try:
            result = json.loads(raw)
            if isinstance(result, dict) and "rewritten_queries" in result:
                return result
        except json.JSONDecodeError:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                result = json.loads(raw[start:end + 1])
                if isinstance(result, dict) and "rewritten_queries" in result:
                    return result
            except json.JSONDecodeError:
                pass

        logger.warning("无法解析 Query Rewrite 结果: %s", raw[:200])
        return {"rewritten_queries": [], "reason": "", "raw": raw}

    # ------------------------------------------------------------------
    # Retry mechanism
    # ------------------------------------------------------------------

    @staticmethod
    def _build_retry_prompt(original_prompt: str, failed_output: str) -> str:
        """Build a retry prompt, using the last failed output as correction context."""
        return (
            "## 原始任务\n"
            f"{original_prompt}\n\n"
            "## 上一次输出（格式不符合 JSON 规范，请修正）\n"
            f"{failed_output}\n\n"
            "## 修正要求\n"
            "请严格按照以下 JSON 格式重新输出，不要包含任何额外内容：\n\n"
            '{"rewritten_queries": ["改写1", "改写2", "改写3"], "reason": "改写理由"}\n\n'
            "注意：\n"
            "1. rewritten_queries 必须是字符串数组，至少包含 1 条改写\n"
            "2. reason 简要说明改写策略\n"
            "3. 只输出 JSON 对象，不要包裹 markdown 代码块或其他文字"
        )

    async def _execute_with_retry(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None, max_retries: int = 1) -> Dict[str, Any]:
        """Run the Query Rewrite call, auto-retrying once when parsing fails.

        Args:
            prompt: full prompt of the first request.
            llm_config: LLM config, passed from ctx.llm_config.
            max_retries: max retry count, default 1.
        """
        raw = await self._call_llm(prompt, llm_config)
        result = self._parse_rewrite_result(raw)

        if "raw" not in result:
            return result

        for attempt in range(1, max_retries + 1):
            logger.warning(
                "Query Rewrite 结果解析失败，第 %d/%d 次重试...",
                attempt, max_retries,
            )
            retry_prompt = self._build_retry_prompt(prompt, raw)
            raw = await self._call_llm(retry_prompt, llm_config)
            result = self._parse_rewrite_result(raw)

            if "raw" not in result:
                logger.info("Query Rewrite 重试成功（第 %d 次）", attempt)
                return result

        logger.warning("Query Rewrite 重试 %d 次后仍失败，返回兜底结果", max_retries)
        return result

    # ------------------------------------------------------------------
    # Prompt template selection (priority: node > module > default)
    # ------------------------------------------------------------------

    def _resolve_prompt_template(self, cxt: DialogueContext) -> str:
        """Resolve the prompt template by priority: node > module > class default."""
        return resolve_prompt_template(
            cxt, "base_query_rewrite_prompt", self._default_prompt_template()
        )

    def _default_prompt_template(self) -> str:
        """Subclasses may override this method to return the default template."""
        return QUERY_REWRITE_DEFAULT_PROMPT

    # ------------------------------------------------------------------
    # Template filling — slot name → data-layer formatting mapping
    # (assembled in node.py / module.py / base.py; this layer only maps)
    # ------------------------------------------------------------------

    @staticmethod
    def _fill_template(template: str, kwargs: Dict[str, str]) -> str:
        """Replace ``{__key__}`` placeholders in the template with their values."""
        return fill_prompt_template(template, kwargs)

    def _build_template_kwargs(self, cxt: DialogueContext) -> Dict[str, str]:
        """Collect all template variables.

        Keys are fixed slot names (without the ``__`` prefix); values come from
        the data-layer formatting methods.
        """
        return {
            "cur_node": cxt.format_cur_node(stage="full"),
            "query": cxt.user_query,
            "history": cxt.format_history(),
            "filled_slots": cxt.format_slots(),
            "recall_info": cxt.format_recall_info(),
        }

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    def prompt_build(self, cxt: DialogueContext) -> str:
        """Build the Query Rewrite prompt and return the formatted string."""
        ...

    @abstractmethod
    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run the Query Rewrite stage and write the result into ctx.rewritten_queries."""
        ...


# ============================================================================
# Default Query Rewriter
# ============================================================================

class QueryRewriter(BaseQueryRewriter):
    """Default Query Rewrite implementation.

    Uses ``QUERY_REWRITE_DEFAULT_PROMPT`` as the default template;
    node/module level override via ``base_query_rewrite_prompt`` supported.

    Rewrite results are written to ``ctx.rewritten_queries``.
    """

    def prompt_build(self, cxt: DialogueContext) -> str:
        prompt_template = self._resolve_prompt_template(cxt)
        kwargs = self._build_template_kwargs(cxt)

        return self._fill_template(prompt_template, kwargs)

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        prompt = self.prompt_build(ctx)
        result = await self._execute_with_retry(prompt, ctx.llm_config)

        rewritten_queries = result.get("rewritten_queries", [])
        rewrite_reason = result.get("reason", "")

        if not rewritten_queries:
            logger.warning(
                "Query Rewrite 未生成改写结果，回退到原始查询: session=%s",
                ctx.session_id,
            )
            rewritten_queries = [ctx.user_query]

        ctx.rewritten_queries = rewritten_queries
        ctx.metadata["rewrite_reason"] = rewrite_reason

        logger.info(
            "Query Rewrite 完成: session=%s, query_count=%d, reason=%s",
            ctx.session_id,
            len(rewritten_queries),
            rewrite_reason,
        )
        return ctx