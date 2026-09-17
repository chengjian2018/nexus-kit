"""
Recall stage — multi-path recall, rule filtering, fusion ranking and reranking.

Supports multiple recall paths, pluggable filters, multiple fusion strategies
and rerankers, composable into a complete recall Pipeline.

Business background:
- Case ES table, fields include but are not limited to: id, pattern_code, single_turn_text,
  case_keyword, single_turn_vec, multi_turn_vec, intent, slots, generate_text
- Knowledge-base ES table, fields include but are not limited to: pattern_code, doc_id, chunk_id,
  chunk_type (policy, faq, etc.), doc_title, doc_title_vec, chunk_text, chunk_vec, chunk_keyword

Architecture::

    MultiPathRecaller (PipelineStage)       ← orchestrator, the only public entry
    ├── RecallPath[]     (multi-path sources) ← vector / ES / keyword / LLM etc.
    ├── BaseFilter[]     (rule filter chain)  ← dedup / threshold / count cutoff / field filter
    ├── BaseFusion       (fusion strategy)    ← RRF / weighted / round-robin
    └── BaseReranker     (reranker)           ← LLM Rerank / MMR diversity / score rerank

LLM call chain: config/local_config.yaml → build_provider → chat_completion.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from nexus.context import (
    DialogueContext,
    PipelineStage,
    fill_prompt_template,
    resolve_prompt_template,
)
from nexus.llm.resolve import build_provider
from atoms.stages._prompts import RECALL_LLM_DEFAULT_PROMPT, RECALL_RERANK_LLM_PROMPT

logger = logging.getLogger(__name__)


# ============================================================================
# Data helpers
# ============================================================================

def _standardize_result(
    item: Dict[str, Any],
    source: str,
    default_score: float = 0.0,
) -> Dict[str, Any]:
    """Standardize a single recall result into the unified format.

    Args:
        item: raw recall item.
        source: recall source path name.
        default_score: default score when the item lacks a ``score`` field.

    Returns:
        standardized dict with ``id``, ``content``, ``score``, ``source``, ``metadata``.
    """
    return {
        "id": item.get("id", ""),
        "content": item.get("content", ""),
        "score": float(item.get("score", default_score)),
        "source": item.get("source", source),
        "metadata": item.get("metadata", {}),
    }


# ============================================================================
# RecallPath — single-path recall abstraction
# ============================================================================

class RecallPath(ABC):
    """Abstract base class for a single recall path.

    Every recall strategy (vector search, ES full-text, knowledge graph, LLM generation, etc.) implements this interface.

    Attributes:
        name: recall path name, used for logging and result tracing.
        weight: path weight, used in the weighted fusion stage.
        top_k: max number of results returned by this path.
    """

    def __init__(self, name: str, weight: float = 1.0, top_k: int = 10):
        self.name = name
        self.weight = weight
        self.top_k = top_k

    @abstractmethod
    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        """Run a single-path recall (async since the asyncio rewrite;
        injected search/call_llm callbacks may be sync OR async — the
        call sites duck-type via inspect.iscoroutinefunction).

        Args:
            query: query text.
            ctx: current dialogue context.
            **kwargs: path-specific extra args.

        Returns:
            list of standardized recall results.
        """
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} weight={self.weight}>"


# ---------------------------------------------------------------------------
# Keyword recall path
# ---------------------------------------------------------------------------

class KeywordRecallPath(RecallPath):
    """Recall path based on keyword matching.

    Uses a simple TF-IDF idea: tokenizes the query and scores documents by
    term frequency-inverse document frequency. Good as a fast baseline path.

    The document store can be preset via ``documents``, or an external search engine
    can be plugged in via ``search_func``.
    """

    def __init__(
        self,
        name: str = "keyword",
        weight: float = 1.0,
        top_k: int = 10,
        documents: Optional[List[Dict[str, Any]]] = None,
        search_func: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ):
        super().__init__(name=name, weight=weight, top_k=top_k)
        self._documents = documents or []
        self._search_func = search_func
        self._inverted_index: Dict[str, List[int]] = {}
        self._build_index()

    def _build_index(self) -> None:
        """Build the inverted index."""
        if not self._documents:
            return
        for idx, doc in enumerate(self._documents):
            content = doc.get("content", "")
            for token in self._tokenize(content):
                self._inverted_index.setdefault(token, []).append(idx)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple tokenization: split on non-alphanumeric chars and lowercase.

        CJK runs have no spaces, so each is emitted as single characters to
        allow cross-string keyword matching (English keeps whole words).
        """
        import re
        tokens = []
        for t in re.findall(r"\w+", text):
            if re.search(r"[一-鿿]", t):
                tokens.extend(t)
            elif len(t) > 1:
                tokens.append(t.lower())
        return tokens

    def add_documents(self, documents: List[Dict[str, Any]]) -> None:
        """Dynamically add documents and update the index."""
        start_idx = len(self._documents)
        self._documents.extend(documents)
        for i, doc in enumerate(documents):
            content = doc.get("content", "")
            for token in self._tokenize(content):
                self._inverted_index.setdefault(token, []).append(start_idx + i)

    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        # Prefer the external search function when provided (sync or async)
        if self._search_func is not None:
            raw = self._search_func(query, top_k=self.top_k, **kwargs)
            if inspect.iscoroutine(raw):
                raw = await raw
            return [_standardize_result(r, source=self.name) for r in raw[: self.top_k]]

        # Use the built-in inverted index
        query_tokens = self._tokenize(query)
        if not query_tokens or not self._documents:
            return []

        scores: Dict[int, float] = {}
        doc_count = len(self._documents)

        for token in query_tokens:
            doc_indices = self._inverted_index.get(token, [])
            idf = math.log((doc_count + 1) / (len(doc_indices) + 1)) + 1.0
            for idx in doc_indices:
                tf = 1.0
                scores[idx] = scores.get(idx, 0.0) + tf * idf

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in ranked[: self.top_k]:
            doc = dict(self._documents[idx])
            doc["score"] = round(score, 4)
            results.append(_standardize_result(doc, source=self.name))

        logger.debug("KeywordRecallPath '%s' 召回 %d 条结果", self.name, len(results))
        return results


# ---------------------------------------------------------------------------
# Embedding recall path
# ---------------------------------------------------------------------------

class EmbeddingRecallPath(RecallPath):
    """Vector search recall path.

    Connects to a vector database (Milvus, Pinecone, FAISS, ES, etc.) through an
    external ``search_func``. No built-in vectorization keeps it decoupled from
    external infrastructure.

    ``search_func`` signature::

        def search_func(query: str, top_k: int, **kwargs) -> List[Dict[str, Any]]:
            ...
    """

    def __init__(
        self,
        name: str = "embedding",
        weight: float = 1.0,
        top_k: int = 10,
        search_func: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ):
        super().__init__(name=name, weight=weight, top_k=top_k)
        self._search_func = search_func

    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        if self._search_func is None:
            logger.warning("EmbeddingRecallPath '%s' 未配置 search_func，返回空结果", self.name)
            return []

        raw = self._search_func(query, top_k=self.top_k, **kwargs)
        if inspect.iscoroutine(raw):
            raw = await raw
        results = [_standardize_result(r, source=self.name) for r in raw[: self.top_k]]
        logger.debug("EmbeddingRecallPath '%s' 召回 %d 条结果", self.name, len(results))
        return results


# ---------------------------------------------------------------------------
# ES recall path
# ---------------------------------------------------------------------------

class ESRecallPath(RecallPath):
    """Elasticsearch recall path.

    Connects to ES through an external ``search_func``, supporting case-base and knowledge-base scenarios:

    - Case base: search by single_turn_text / case_keyword etc.
    - Knowledge base: search by chunk_text / chunk_keyword / doc_title etc.

    ``search_func`` signature::

        def search_func(query: str, top_k: int, **kwargs) -> List[Dict[str, Any]]:
            ...
    """

    def __init__(
        self,
        name: str = "es",
        weight: float = 1.0,
        top_k: int = 10,
        search_func: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        index_type: Literal["case", "knowledge"] = "knowledge",
    ):
        super().__init__(name=name, weight=weight, top_k=top_k)
        self._search_func = search_func
        self.index_type = index_type

    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        if self._search_func is None:
            logger.warning("ESRecallPath '%s' 未配置 search_func，返回空结果", self.name)
            return []

        raw = self._search_func(
            query, top_k=self.top_k, index_type=self.index_type, **kwargs
        )
        if inspect.iscoroutine(raw):
            raw = await raw
        results = [_standardize_result(r, source=self.name) for r in raw[: self.top_k]]
        logger.debug("ESRecallPath '%s' (%s) 召回 %d 条结果", self.name, self.index_type, len(results))
        return results


# ---------------------------------------------------------------------------
# LLM recall path
# ---------------------------------------------------------------------------

class LLMRecallPath(RecallPath):
    """LLM-based knowledge recall path.

    Generates relevant snippets directly from built-in knowledge via the LLM.
    Suits small knowledge bases that need reasoning-based generation.

    ``call_llm`` callback signature::

        def call_llm(prompt: str) -> str:
            ...
    """

    def __init__(
        self,
        name: str = "llm",
        weight: float = 1.0,
        top_k: int = 10,
        call_llm: Optional[Callable[[str], str]] = None,
        prompt_template: Optional[str] = None,
    ):
        super().__init__(name=name, weight=weight, top_k=top_k)
        self._call_llm = call_llm
        self._prompt_template = prompt_template or RECALL_LLM_DEFAULT_PROMPT

    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        if self._call_llm is None:
            logger.warning("LLMRecallPath '%s' 未配置 call_llm，返回空结果", self.name)
            return []

        # node-level override resolved by the orchestrator takes priority
        template = kwargs.pop("prompt_template", None) or self._prompt_template

        slots = {
            "query": query,
            "recall_count": str(self.top_k),
            "history": ctx.format_history(),
            "cur_node": kwargs.pop("cur_node", "暂无当前节点信息"),
            "filled_slots": ctx.format_slots(),
        }
        prompt = fill_prompt_template(template, slots)

        raw_response = self._call_llm(prompt)
        if inspect.iscoroutine(raw_response):
            raw_response = await raw_response

        try:
            parsed = self._parse_llm_response(raw_response)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("LLMRecallPath '%s' 解析失败: %s", self.name, e)
            return []

        results = [_standardize_result(r, source=self.name) for r in parsed[: self.top_k]]
        logger.debug("LLMRecallPath '%s' 召回 %d 条结果", self.name, len(results))
        return results

    @staticmethod
    def _parse_llm_response(raw: str) -> List[Dict[str, Any]]:
        """Parse the JSON array returned by the LLM."""
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start: end + 1])

        raise ValueError(f"无法解析 LLM 响应为 JSON 数组: {raw[:200]}")


# ---------------------------------------------------------------------------
# Custom recall path
# ---------------------------------------------------------------------------

class CustomRecallPath(RecallPath):
    """Custom recall path wrapping any callable.

    Maximum flexibility: pass any ``callable`` and it becomes a recall path.

    ``recall_func`` signature::

        def recall_func(query: str, ctx: DialogueContext, top_k: int, **kwargs) -> List[Dict[str, Any]]:
            ...
    """

    def __init__(
        self,
        name: str = "custom",
        weight: float = 1.0,
        top_k: int = 10,
        recall_func: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ):
        super().__init__(name=name, weight=weight, top_k=top_k)
        self._recall_func = recall_func

    async def recall(
        self, query: str, ctx: DialogueContext, **kwargs
    ) -> List[Dict[str, Any]]:
        if self._recall_func is None:
            logger.warning("CustomRecallPath '%s' 未配置 recall_func，返回空结果", self.name)
            return []

        raw = self._recall_func(query=query, ctx=ctx, top_k=self.top_k, **kwargs)
        if inspect.iscoroutine(raw):
            raw = await raw
        results = [_standardize_result(r, source=self.name) for r in raw[: self.top_k]]
        logger.debug("CustomRecallPath '%s' 召回 %d 条结果", self.name, len(results))
        return results


# ============================================================================
# BaseFilter — post-recall filtering rules
# ============================================================================

class BaseFilter(ABC):
    """Abstract base class for recall result filters.

    Filters single- or multi-path recall results before fusion, removing low-quality or duplicate items.
    """

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        """Filter recall results.

        Args:
            results: recall result list to filter.
            ctx: current dialogue context.

        Returns:
            filtered result list.
        """
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Dedup filter
# ---------------------------------------------------------------------------

class DedupFilter(BaseFilter):
    """Dedup filter.

    Dedups by ``id`` or ``content`` hash, keeping the highest-scored item.
    """

    def __init__(self, by: Literal["id", "content"] = "id", name: str = ""):
        super().__init__(name=name)
        self.by = by

    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        for item in results:
            if self.by == "id":
                key = str(item.get("id", ""))
                if not key:
                    key = hashlib.md5(
                        item.get("content", "").encode("utf-8")
                    ).hexdigest()
            else:
                key = hashlib.md5(
                    item.get("content", "").encode("utf-8")
                ).hexdigest()

            if key not in seen or item.get("score", 0.0) > seen[key].get("score", 0.0):
                seen[key] = item

        filtered = list(seen.values())
        removed = len(results) - len(filtered)
        if removed > 0:
            logger.debug("DedupFilter 去重: %d -> %d (移除 %d 条)", len(results), len(filtered), removed)
        return filtered


# ---------------------------------------------------------------------------
# Score threshold filter
# ---------------------------------------------------------------------------

class ScoreThresholdFilter(BaseFilter):
    """Score threshold filter.

    Drops recall results scored below ``threshold``.
    """

    def __init__(self, threshold: float = 0.3, name: str = ""):
        super().__init__(name=name)
        self.threshold = threshold

    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        filtered = [r for r in results if r.get("score", 0.0) >= self.threshold]
        removed = len(results) - len(filtered)
        if removed > 0:
            logger.debug(
                "ScoreThresholdFilter(>=%.2f): %d -> %d (移除 %d 条)",
                self.threshold, len(results), len(filtered), removed,
            )
        return filtered


# ---------------------------------------------------------------------------
# Max results filter
# ---------------------------------------------------------------------------

class MaxResultsFilter(BaseFilter):
    """Max results filter.

    Truncates results to Top-N: sorts by score descending and keeps the first N.
    """

    def __init__(self, max_results: int = 50, name: str = ""):
        super().__init__(name=name)
        self.max_results = max_results

    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        if len(results) <= self.max_results:
            return results
        sorted_results = sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)
        return sorted_results[: self.max_results]


# ---------------------------------------------------------------------------
# Field filter
# ---------------------------------------------------------------------------

class FieldFilter(BaseFilter):
    """Filter by metadata field conditions.

    Suits knowledge-base ES tables, e.g. filtering by ``chunk_type`` (keep only
    "policy" or "FAQ"), or by ``pattern_code`` to keep only results matching the
    current business module.

    Args:
        include_rules: whitelist rules, dict format ``{"field": "value"}``;
            results must satisfy all rules to be kept.
        exclude_rules: blacklist rules, dict format ``{"field": "value"}``;
            results matching any rule are excluded.
    """

    def __init__(
        self,
        include_rules: Optional[Dict[str, Any]] = None,
        exclude_rules: Optional[Dict[str, Any]] = None,
        name: str = "",
    ):
        super().__init__(name=name)
        self.include_rules = include_rules or {}
        self.exclude_rules = exclude_rules or {}

    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        filtered = []
        for item in results:
            metadata = item.get("metadata", {})

            if self.exclude_rules:
                excluded = any(
                    metadata.get(k) == v for k, v in self.exclude_rules.items()
                )
                if excluded:
                    continue

            if self.include_rules:
                included = all(
                    metadata.get(k) == v for k, v in self.include_rules.items()
                )
                if not included:
                    continue

            filtered.append(item)

        removed = len(results) - len(filtered)
        if removed > 0:
            logger.debug("FieldFilter: %d -> %d (移除 %d 条)", len(results), len(filtered), removed)
        return filtered


# ---------------------------------------------------------------------------
# Filter chain
# ---------------------------------------------------------------------------

class FilterChain(BaseFilter):
    """Filter chain, runs multiple filters in order.

    Usage example::

        chain = FilterChain([
            DedupFilter(by="id"),
            ScoreThresholdFilter(threshold=0.3),
            FieldFilter(include_rules={"chunk_type": "faq"}),
            MaxResultsFilter(max_results=50),
        ])
        results = chain.filter(results, ctx)
    """

    def __init__(self, filters: Optional[List[BaseFilter]] = None, name: str = ""):
        super().__init__(name=name or "FilterChain")
        self.filters = filters or []

    def add(self, f: BaseFilter) -> "FilterChain":
        """Append a filter to the chain."""
        self.filters.append(f)
        return self

    def filter(
        self, results: List[Dict[str, Any]], ctx: DialogueContext
    ) -> List[Dict[str, Any]]:
        for f in self.filters:
            results = f.filter(results, ctx)
        return results


# ============================================================================
# BaseFusion — multi-path result fusion and ranking
# ============================================================================

class BaseFusion(ABC):
    """Abstract base class for multi-path recall fusion strategies.

    Merges results from multiple recall paths into one ordered list.
    """

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    def fuse(
        self,
        path_results: List[Tuple[str, List[Dict[str, Any]]]],
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        """Fuse multi-path recall results.

        Args:
            path_results: list of ``(path name, that path's recall result list)``.
            ctx: current dialogue context.

        Returns:
            fused and ranked result list.
        """
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion (RRF)
# ---------------------------------------------------------------------------

class ReciprocalRankFusion(BaseFusion):
    """RRF (Reciprocal Rank Fusion) strategy.

    Sums the reciprocal of each item's rank across paths; the k parameter smooths the result.
    Formula: ``score = Σ 1 / (k + rank_i)``

    Suits scenarios where scores from different paths are not directly comparable
    (different models, different scales).

    Args:
        k: smoothing parameter, default 60 (classic value).
    """

    def __init__(self, k: int = 60, name: str = ""):
        super().__init__(name=name)
        self.k = k

    def fuse(
        self,
        path_results: List[Tuple[str, List[Dict[str, Any]]]],
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        rrf_scores: Dict[str, float] = {}
        id_to_item: Dict[str, Dict[str, Any]] = {}

        for _path_name, results in path_results:
            sorted_results = sorted(
                results, key=lambda r: r.get("score", 0.0), reverse=True
            )
            for rank, item in enumerate(sorted_results, start=1):
                item_id = item.get("id", "") or item.get("content", "")
                rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (self.k + rank)
                if item_id not in id_to_item:
                    id_to_item[item_id] = dict(item)

        ranked_ids = sorted(rrf_scores, key=lambda i: rrf_scores[i], reverse=True)
        fused = []
        for item_id in ranked_ids:
            item = dict(id_to_item[item_id])
            item["score"] = round(rrf_scores[item_id], 6)
            fused.append(item)

        logger.debug("RRF 融合: %d 路 -> %d 条结果", len(path_results), len(fused))
        return fused


# ---------------------------------------------------------------------------
# Weighted score fusion
# ---------------------------------------------------------------------------

class WeightedScoreFusion(BaseFusion):
    """Weighted score fusion strategy.

    Weighted-sums scores using each path's ``weight``. Scores of the same item
    (deduped by id) appearing in multiple paths accumulate.

    Args:
        path_weights: path-name-to-weight mapping; unspecified paths default to 1.0.
    """

    def __init__(
        self,
        path_weights: Optional[Dict[str, float]] = None,
        name: str = "",
    ):
        super().__init__(name=name)
        self.path_weights = path_weights or {}

    def fuse(
        self,
        path_results: List[Tuple[str, List[Dict[str, Any]]]],
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        id_to_item: Dict[str, Dict[str, Any]] = {}
        id_to_score: Dict[str, float] = {}

        for path_name, results in path_results:
            weight = self.path_weights.get(path_name, 1.0)
            for item in results:
                item_id = item.get("id", "") or item.get("content", "")
                weighted_score = item.get("score", 0.0) * weight
                id_to_score[item_id] = id_to_score.get(item_id, 0.0) + weighted_score
                if item_id not in id_to_item or item.get("score", 0.0) > id_to_item[item_id].get("score", 0.0):
                    id_to_item[item_id] = dict(item)

        ranked_ids = sorted(id_to_score, key=lambda i: id_to_score[i], reverse=True)
        fused = []
        for item_id in ranked_ids:
            item = dict(id_to_item[item_id])
            item["score"] = round(id_to_score[item_id], 6)
            fused.append(item)

        logger.debug("WeightedScoreFusion: %d 路 -> %d 条结果", len(path_results), len(fused))
        return fused


# ---------------------------------------------------------------------------
# Round-robin fusion
# ---------------------------------------------------------------------------

class RoundRobinFusion(BaseFusion):
    """Round-robin fusion strategy.

    Picks results from each path in alternation, ensuring even distribution of
    multi-path results in the final list. Suits scenarios demanding diversity
    where scores are not directly comparable.
    """

    def __init__(self, name: str = ""):
        super().__init__(name=name)

    def fuse(
        self,
        path_results: List[Tuple[str, List[Dict[str, Any]]]],
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        sorted_paths = [
            sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)
            for _, results in path_results
        ]

        seen: set = set()
        fused: List[Dict[str, Any]] = []
        pointers = [0] * len(sorted_paths)
        active = len(sorted_paths) > 0

        while active:
            active = False
            for i, path_results_list in enumerate(sorted_paths):
                if pointers[i] < len(path_results_list):
                    active = True
                    item = path_results_list[pointers[i]]
                    item_id = item.get("id", "") or item.get("content", "")
                    if item_id not in seen:
                        seen.add(item_id)
                        fused.append(dict(item))
                    pointers[i] += 1

        logger.debug("RoundRobinFusion: %d 路 -> %d 条结果", len(path_results), len(fused))
        return fused


# ============================================================================
# BaseReranker — reranker
# ============================================================================

class BaseReranker(ABC):
    """Abstract base class for rerankers.

    Reranks fused candidate results — the last step of the recall flow.
    """

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    async def rerank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        """Rerank the fused results.

        Args:
            results: candidate result list to rerank.
            query: query text.
            ctx: current dialogue context.

        Returns:
            reranked result list.
        """
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Score reranker (baseline)
# ---------------------------------------------------------------------------

class ScoreBasedReranker(BaseReranker):
    """Rerank by score descending (baseline implementation).

    Optionally multiplies path weights for score adjustment.
    """

    def __init__(
        self,
        path_weights: Optional[Dict[str, float]] = None,
        name: str = "",
    ):
        super().__init__(name=name)
        self.path_weights = path_weights or {}

    async def rerank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        weighted = []
        for item in results:
            source = item.get("source", "")
            weight = self.path_weights.get(source, 1.0)
            item_copy = dict(item)
            item_copy["score"] = item.get("score", 0.0) * weight
            weighted.append(item_copy)

        return sorted(weighted, key=lambda r: r.get("score", 0.0), reverse=True)


# ---------------------------------------------------------------------------
# MMR diversity reranker
# ---------------------------------------------------------------------------

class DiversityReranker(BaseReranker):
    """MMR (Maximal Marginal Relevance) diversity reranker.

    Boosts final result diversity while preserving relevance by penalizing
    candidates too similar to already-selected items. Suits knowledge-base recall
    where highly duplicated snippets should be avoided.

    Args:
        lambda_param: relevance-diversity trade-off. 0 = max diversity, 1 = max relevance.
    """

    def __init__(
        self,
        lambda_param: float = 0.7,
        name: str = "",
    ):
        super().__init__(name=name)
        self.lambda_param = lambda_param

    async def rerank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        if not results:
            return []

        pool = sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)
        selected: List[Dict[str, Any]] = [pool[0]]
        remaining = pool[1:]

        while remaining:
            mmr_scores = []
            for item in remaining:
                relevance = item.get("score", 0.0)
                max_sim = max(
                    self._jaccard_similarity(item, selected_item)
                    for selected_item in selected
                )
                mmr = self.lambda_param * relevance - (1 - self.lambda_param) * max_sim
                mmr_scores.append(mmr)

            best_idx = max(range(len(mmr_scores)), key=lambda i: mmr_scores[i])
            selected.append(remaining.pop(best_idx))

        logger.debug("DiversityReranker(MMR): 重排 %d 条结果", len(selected))
        return selected

    @staticmethod
    def _jaccard_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        """Compute Jaccard similarity of two results' content (char-level bigram)."""
        content_a = a.get("content", "")
        content_b = b.get("content", "")

        if not content_a or not content_b:
            return 0.0

        def bigrams(text: str) -> set:
            return set(text[i: i + 2] for i in range(len(text) - 1))

        set_a = bigrams(content_a)
        set_b = bigrams(content_b)

        intersection = len(set_a & set_b)
        union = len(set_a | set_b)

        return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# LLM reranker
# ---------------------------------------------------------------------------

class LLMReranker(BaseReranker):
    """LLM-based reranker.

    Uses the LLM to rerank the candidate list and output rescored results.
    Suits scenarios with high ranking-quality requirements and a manageable
    candidate count (≤ 30 recommended).

    ``call_llm`` callback signature::

        def call_llm(prompt: str) -> str:
            ...
    """

    def __init__(
        self,
        call_llm: Optional[Callable[[str], str]] = None,
        prompt_template: Optional[str] = None,
        name: str = "",
    ):
        super().__init__(name=name)
        self._call_llm = call_llm
        self._prompt_template = prompt_template or RECALL_RERANK_LLM_PROMPT

    async def rerank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        ctx: DialogueContext,
    ) -> List[Dict[str, Any]]:
        if self._call_llm is None:
            logger.warning("LLMReranker 未配置 call_llm，回退到原始顺序")
            return results

        if not results:
            return []

        candidates_text = json.dumps(
            [
                {"id": r.get("id", ""), "content": r.get("content", "")[:200]}
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
        )

        prompt = self._prompt_template.replace("{__query__}", query)
        prompt = prompt.replace("{__candidates__}", candidates_text)

        raw_response = self._call_llm(prompt)
        if inspect.iscoroutine(raw_response):
            raw_response = await raw_response

        try:
            reranked_data = self._parse_rerank_response(raw_response)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("LLMReranker 解析失败: %s，回退到原始顺序", e)
            return results

        id_to_item = {r.get("id", ""): r for r in results}
        reranked = []
        for entry in reranked_data:
            item_id = entry.get("id", "")
            if item_id in id_to_item:
                item = dict(id_to_item[item_id])
                item["score"] = float(entry.get("score", item.get("score", 0.0)))
                item["metadata"] = dict(item.get("metadata", {}))
                item["metadata"]["rerank_reason"] = entry.get("reason", "")
                reranked.append(item)

        # Append items the LLM did not return (keep original order)
        returned_ids = {e.get("id", "") for e in reranked_data}
        for r in results:
            if r.get("id", "") not in returned_ids:
                reranked.append(dict(r))

        logger.debug("LLMReranker: 重排 %d 条结果", len(reranked))
        return reranked

    @staticmethod
    def _parse_rerank_response(raw: str) -> List[Dict[str, Any]]:
        """Parse the LLM rerank response."""
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start: end + 1])

        raise ValueError(f"无法解析 LLM Rerank 响应: {raw[:200]}")


# ============================================================================
# MultiPathRecaller — multi-path recall orchestrator
# ============================================================================

class MultiPathRecaller(PipelineStage):
    """Multi-path recall orchestrator, integrated into the Pipeline system.

    Composes RecallPath, Filter, Fusion and Reranker into a complete recall flow.
    Inherits ``PipelineStage``, so it can be placed directly in a Pattern's stages list.

    Execution flow::

        1. Determine query text (pre uses user_query, post uses rewritten_queries)
        2. Run all RecallPath.recall() serially
        3. Run the filter chain in order
        4. Run the fusion strategy
        5. Run reranking
        6. Write to ctx.pre_recall_results or ctx.post_recall_results

    Usage example::

        # case-base + knowledge-base dual-path recall
        recaller = MultiPathRecaller(
            recall_paths=[
                ESRecallPath(
                    name="case_es",
                    index_type="case",
                    weight=0.6,
                    search_func=case_es_search,
                ),
                ESRecallPath(
                    name="kb_es",
                    index_type="knowledge",
                    weight=1.0,
                    search_func=kb_es_search,
                ),
            ],
            filters=[
                DedupFilter(by="id"),
                ScoreThresholdFilter(threshold=0.3),
                FieldFilter(include_rules={"chunk_type": "faq"}),
            ],
            fusion=ReciprocalRankFusion(k=60),
            reranker=ScoreBasedReranker(),
            phase="pre",
        )
        ctx = recaller.execute(ctx)
    """

    stage_name = "recall"

    def __init__(
        self,
        recall_paths: List[RecallPath],
        filters: Optional[List[BaseFilter]] = None,
        fusion: Optional[BaseFusion] = None,
        reranker: Optional[BaseReranker] = None,
        phase: Literal["pre", "post"] = "pre",
    ):
        """Initialize the multi-path recall orchestrator.

        Args:
            recall_paths: list of recall paths.
            filters: filter list, executed in order.
            fusion: fusion strategy, default ``WeightedScoreFusion``.
            reranker: reranker, default ``ScoreBasedReranker``.
            phase: recall phase, ``"pre"`` before rewrite, ``"post"`` after rewrite.
        """
        self.recall_paths = recall_paths
        self._filters = filters or []
        self._fusion = fusion or WeightedScoreFusion()
        self._reranker = reranker or ScoreBasedReranker()
        self.phase = phase

        # Components that got auto-injected LLM callbacks (rebound per execute via ctx.llm_config)
        self._auto_injected: List[Any] = []

    # ------------------------------------------------------------------
    # LLM client (reuses the NLU/NLG pattern)
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
        logger.debug("Recaller LLM 返回: %s", content[:200])
        return content

    def _inject_llm_callbacks(self, llm_config: Optional[Dict[str, Any]]) -> None:
        """Inject LLM callbacks into components that need the LLM.

        For LLMRecallPath and LLMReranker without an explicit call_llm, auto-bind
        to self._call_llm for unified LLM management (async since the asyncio
        rewrite; consumers duck-type the return via inspect.iscoroutine).

        Called on every ``execute``: auto-injected components are rebound to the
        current ``ctx.llm_config``; components with an explicit call_llm are untouched.
        """
        async def callback(prompt: str) -> str:
            return await self._call_llm(prompt, llm_config)

        for path in self.recall_paths:
            if not isinstance(path, LLMRecallPath):
                continue
            if path._call_llm is not None and path not in self._auto_injected:
                continue
            path._call_llm = callback
            if path not in self._auto_injected:
                self._auto_injected.append(path)

        if isinstance(self._reranker, LLMReranker):
            if self._reranker._call_llm is None or self._reranker in self._auto_injected:
                self._reranker._call_llm = callback
                if self._reranker not in self._auto_injected:
                    self._auto_injected.append(self._reranker)

    # ------------------------------------------------------------------
    # Prompt template selection (priority: node > default)
    # ------------------------------------------------------------------

    def _resolve_prompt_template(self, ctx: DialogueContext) -> Optional[str]:
        """Resolve the recall prompt template by priority.

        Priority: node level > None (use each path's built-in template).
        """
        return resolve_prompt_template(ctx, "base_recall_prompt", None)

    # ------------------------------------------------------------------
    # Prompt slots for LLM-based paths (node / ctx data-layer formatting)
    # ------------------------------------------------------------------

    def _build_prompt_slots(self, ctx: DialogueContext) -> Dict[str, str]:
        """Build the fixed prompt slot values passed down to LLM recall paths."""
        return {
            "cur_node": ctx.format_cur_node(stage="full"),
            "filled_slots": ctx.format_slots(),
            "history": ctx.format_history(),
        }

    # ------------------------------------------------------------------
    # Query text selection
    # ------------------------------------------------------------------

    def _get_query(self, ctx: DialogueContext) -> str:
        """Get the query text by phase.

        The pre phase uses the original query; the post phase prefers the rewritten queries.
        """
        if self.phase == "post" and ctx.rewritten_queries:
            return " ".join(ctx.rewritten_queries)
        return ctx.user_query

    # ------------------------------------------------------------------
    # PipelineStage interface
    # ------------------------------------------------------------------

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run the complete multi-path recall flow.

        Args:
            ctx: current dialogue context.

        Returns:
            updated dialogue context; recall results are written to
            ``ctx.pre_recall_results`` or ``ctx.post_recall_results``.
        """
        # Inject/refresh LLM callbacks per the current ctx's llm_config
        self._inject_llm_callbacks(ctx.llm_config)

        # Resolve node-level recall prompt override and slot values once per execute
        prompt_template = self._resolve_prompt_template(ctx)
        prompt_slots = self._build_prompt_slots(ctx)

        query = self._get_query(ctx)
        logger.info(
            "Recaller 开始执行: session=%s, phase=%s, paths=%d, query_len=%d",
            ctx.session_id, self.phase, len(self.recall_paths), len(query),
        )

        # 1. Run multi-path recall
        path_results: List[Tuple[str, List[Dict[str, Any]]]] = []
        all_raw: List[Dict[str, Any]] = []

        for path in self.recall_paths:
            logger.debug("执行召回路径: %s", path.name)
            try:
                results = await path.recall(
                    query, ctx, prompt_template=prompt_template, **prompt_slots
                )
                path_results.append((path.name, results))
                all_raw.extend(results)
            except Exception as e:
                logger.error("召回路径 '%s' 执行异常: %s", path.name, e, exc_info=True)
                path_results.append((path.name, []))

        logger.info("多路召回完成: 共 %d 条原始结果", len(all_raw))

        # 2. Run the filter chain
        filtered = all_raw
        for f in self._filters:
            try:
                filtered = f.filter(filtered, ctx)
            except Exception as e:
                logger.error("过滤器 '%s' 执行异常: %s", f.name, e, exc_info=True)

        # 3. Run fusion
        try:
            fused = self._fusion.fuse(path_results, ctx)
        except Exception as e:
            logger.error("融合策略 '%s' 执行异常: %s", self._fusion.name, e, exc_info=True)
            fused = filtered

        # 4. Run reranking
        try:
            final = await self._reranker.rerank(fused, query, ctx)
        except Exception as e:
            logger.error("重排序器 '%s' 执行异常: %s", self._reranker.name, e, exc_info=True)
            final = fused

        # 5. Write to context
        if self.phase == "post":
            ctx.post_recall_results = final
        else:
            ctx.pre_recall_results = final

        logger.info(
            "Recaller 完成: session=%s, phase=%s, final_count=%d",
            ctx.session_id, self.phase, len(final),
        )
        return ctx


# ============================================================================
# Convenience subclasses
# ============================================================================

class PreRecaller(MultiPathRecaller):
    """Pre-rewrite recaller.

    Runs before query rewrite, using the original user query.
    Results are written to ``ctx.pre_recall_results``.
    """

    stage_name = "pre_recall"

    def __init__(
        self,
        recall_paths: List[RecallPath],
        filters: Optional[List[BaseFilter]] = None,
        fusion: Optional[BaseFusion] = None,
        reranker: Optional[BaseReranker] = None,
    ):
        super().__init__(
            recall_paths=recall_paths,
            filters=filters,
            fusion=fusion,
            reranker=reranker,
            phase="pre",
        )


class PostRecaller(MultiPathRecaller):
    """Post-rewrite recaller.

    Runs after query rewrite, using the rewritten queries.
    Results are written to ``ctx.post_recall_results``.
    """

    stage_name = "post_recall"

    def __init__(
        self,
        recall_paths: List[RecallPath],
        filters: Optional[List[BaseFilter]] = None,
        fusion: Optional[BaseFusion] = None,
        reranker: Optional[BaseReranker] = None,
    ):
        super().__init__(
            recall_paths=recall_paths,
            filters=filters,
            fusion=fusion,
            reranker=reranker,
            phase="post",
        )