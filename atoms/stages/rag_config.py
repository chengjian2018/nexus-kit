"""RAG pipeline declarative config — the retrieval-config backend of the
ops console.

Upgrades the clarify recall pipeline (MultiPathRecaller + ClarifyRouteRule
+ ClarifyStage) from Python constructor args to declarative yml config
(PRD ops-console §6.4 / retrieval-parameter gap), providing four
capabilities:

1. ``normalize_rag_config`` — validate + fill defaults (collecting
   errors, same style as model/validation.py: list every problem at
   once);
2. ``build_clarify_stage`` — declaration → component assembly (recall
   paths / filter chain / fusion / reranker / gating rule);
3. ``apply_rag_config`` / ``reset_rag_config`` — re-register via the
   plugin registry (``rag_clarify`` explicit code + ``clarify_default``
   override + the builtin clarify factory), effective on the next
   dialogue turn (stages resolve per turn; the instance cache is cleared
   by deregister);
4. ``test_run_rag`` — offline dry run: only recall + gating (both are
   zero-LLM), deterministically showing "what happens when ops changes
   the parameters".

Recall paths v1 supports two real data sources
(KeywordRecallPath.search_func injection, data from the SQLite knowledge
base):

- ``kb_cs``        CS knowledge: title+content searched, tags → business
                   keywords (what ClarifyRouteRule's keyword_bonus
                   boost applies to);
- ``kb_products``  product knowledge: goods_name+extracted_content
                   searched.

Scoring heuristic (shared by both kb paths, shown as-is on the UI dry-run
panel)::

    score = (title hit count × 2 + body hit count) / (query token count × 3), capped at 1.0

So "all tokens hit the title" ≈ 0.67 (≥ default t_high=0.6 → kb mode),
"all tokens hit only the body" ≈ 0.33 (≥ default t_low=0.3 → mixed mode)
— naturally aligned with the default gating thresholds. embedding / ES /
LLM paths and LLM reranking need external backends, not in v1 (an
unknown registered type errors outright, never silently ignored).

Config file: ``host/config/rag.yaml`` (``NEXUS_RAG_CONFIG`` overrides the
path). A missing file = zero behavior change (the builtin default
assembly stays as-is); an existing file is applied at startup by
``load_and_apply_rag_config()`` (called at the host.main assembly point).

Effective scope (as-is): patterns whose clarify slot declares
``rag_clarify`` / ``clarify_default`` / ``builtin:clarify``. The
install/repair FAQ clarify (``install_clarify`` / ``repair_clarify``) is
the apps' own keyword-gated assembly, not governed by this config.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from atoms.knowledge.store import _cut_query, get_knowledge_store
from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage
from atoms.stages.recaller import (
    DedupFilter,
    DiversityReranker,
    KeywordRecallPath,
    MaxResultsFilter,
    MultiPathRecaller,
    ReciprocalRankFusion,
    RoundRobinFusion,
    ScoreBasedReranker,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)
from nexus.context import DialogueContext
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

# A declarative expression fully equivalent to
# atoms/stages/__init__._default_clarify_stage (empty recall paths =
# nothing recalled, so gating naturally falls to fallback).
DEFAULT_CONFIG: Dict[str, Any] = {
    "recall_paths": [],
    "filters": [{"type": "score_threshold", "threshold": 0.1}],
    "fusion": {"type": "weighted"},
    "reranker": {"type": "score"},
    "rule": {"t_high": 0.6, "t_low": 0.3, "keyword_bonus": 0.1},
}

_PATH_TYPES = ("kb_cs", "kb_products")
_FILTER_TYPES = ("dedup", "score_threshold", "max_results")
_FUSION_TYPES = ("weighted", "rrf", "round_robin")
# Note: MultiPathRecaller's constructor falls back to ScoreBasedReranker
# on reranker=None — "no reranking" does not exist, hence no none option
_RERANKER_TYPES = ("score", "diversity")

_SCOPE_RE = re.compile(r"^[^:\s]+:[^:\s]+$")


def rag_config_path() -> Path:
    """Config file path: NEXUS_RAG_CONFIG overrides, default
    host/config/rag.yaml."""
    override = os.environ.get("NEXUS_RAG_CONFIG", "")
    if override:
        return Path(override)
    return Path("host/config/rag.yaml")


# ---------------------------------------------------------------------------
# Validate + fill defaults (collecting)
# ---------------------------------------------------------------------------

def _check_number(value: Any, lo: float, hi: float, field: str,
                  errors: List[str]) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} 必须是数字: {value!r}")
        return lo
    if not (lo <= num <= hi):
        errors.append(f"{field} 超出范围 [{lo}, {hi}]: {num}")
    return num


def normalize_rag_config(raw: Any) -> Dict[str, Any]:
    """Validate and complete a RAG config; collect all errors and raise
    ValueError once."""
    errors: List[str] = []
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError(f"RAG 配置必须是映射: {type(raw).__name__}")

    known_top = {"recall_paths", "filters", "fusion", "reranker", "rule"}
    unknown_top = set(raw) - known_top
    if unknown_top:
        errors.append(f"未知顶层字段: {sorted(unknown_top)}"
                      f"（合法: {sorted(known_top)}）")

    # --- recall paths ---
    paths_out: List[Dict[str, Any]] = []
    names_seen = set()
    for i, path in enumerate(raw.get("recall_paths") or []):
        label = f"recall_paths[{i}]"
        if not isinstance(path, dict):
            errors.append(f"{label} 必须是映射")
            continue
        unknown = set(path) - {"type", "name", "scope", "weight", "top_k"}
        if unknown:
            errors.append(f"{label} 未知字段: {sorted(unknown)}")
        ptype = path.get("type")
        if ptype not in _PATH_TYPES:
            errors.append(f"{label}.type 必须是 {_PATH_TYPES} 之一: {ptype!r}"
                          "（embedding/ES/LLM 通路需要外部后端，v1 未开放）")
            continue
        name = str(path.get("name") or ptype)
        if name in names_seen:
            errors.append(f"{label}.name 重复: {name!r}（融合权重按 name 索引）")
        names_seen.add(name)
        scope = str(path.get("scope") or "")
        if not _SCOPE_RE.match(scope):
            errors.append(f"{label}.scope 格式必须为 {{channel}}:{{account_id}}: {scope!r}")
        paths_out.append({
            "type": ptype,
            "name": name,
            "scope": scope,
            "weight": _check_number(path.get("weight", 1.0), 0.0, 10.0,
                                    f"{label}.weight", errors),
            "top_k": int(_check_number(path.get("top_k", 5), 1, 50,
                                       f"{label}.top_k", errors)),
        })

    # --- filter chain (ordered; absent = default threshold filter filled
    # in, explicit [] = no filtering) ---
    filters_raw = raw.get("filters")
    if filters_raw is None:
        filters_out = [{"type": "score_threshold", "threshold": 0.1}]
        filters_raw = []
    else:
        filters_out = []
    for i, flt in enumerate(filters_raw):
        label = f"filters[{i}]"
        if not isinstance(flt, dict):
            errors.append(f"{label} 必须是映射")
            continue
        ftype = flt.get("type")
        if ftype not in _FILTER_TYPES:
            errors.append(f"{label}.type 必须是 {_FILTER_TYPES} 之一: {ftype!r}")
            continue
        if ftype == "dedup":
            by = flt.get("by", "id")
            if by not in ("id", "content"):
                errors.append(f"{label}.by 必须是 id/content: {by!r}")
                continue
            filters_out.append({"type": "dedup", "by": by})
        elif ftype == "score_threshold":
            filters_out.append({"type": "score_threshold",
                                "threshold": _check_number(
                                    flt.get("threshold", 0.3), 0.0, 1.0,
                                    f"{label}.threshold", errors)})
        else:  # max_results
            filters_out.append({"type": "max_results",
                                "max_results": int(_check_number(
                                    flt.get("max_results", 50), 1, 100,
                                    f"{label}.max_results", errors))})

    # --- fusion ---
    fusion_raw = raw.get("fusion") or {}
    if not isinstance(fusion_raw, dict):
        errors.append("fusion 必须是映射")
        fusion_out: Dict[str, Any] = {"type": "weighted"}
    else:
        ftype = fusion_raw.get("type", "weighted")
        if ftype not in _FUSION_TYPES:
            errors.append(f"fusion.type 必须是 {_FUSION_TYPES} 之一: {ftype!r}")
            fusion_out = {"type": "weighted"}
        elif ftype == "rrf":
            fusion_out = {"type": "rrf",
                          "k": int(_check_number(fusion_raw.get("k", 60), 1,
                                                 1000, "fusion.k", errors))}
        else:
            fusion_out = {"type": ftype}

    # --- reranker ---
    reranker_raw = raw.get("reranker") or {}
    if not isinstance(reranker_raw, dict):
        errors.append("reranker 必须是映射")
        reranker_out: Dict[str, Any] = {"type": "score"}
    else:
        rtype = reranker_raw.get("type", "score")
        if rtype not in _RERANKER_TYPES:
            errors.append(f"reranker.type 必须是 {_RERANKER_TYPES} 之一: {rtype!r}"
                          "（无重排选项不存在：构造器对 None 回落 score）")
            reranker_out = {"type": "score"}
        elif rtype == "diversity":
            reranker_out = {"type": "diversity",
                            "lambda_param": _check_number(
                                reranker_raw.get("lambda_param", 0.7), 0.0, 1.0,
                                "reranker.lambda_param", errors)}
        else:
            reranker_out = {"type": rtype}

    # --- gating rule ---
    rule_raw = raw.get("rule") or {}
    if not isinstance(rule_raw, dict):
        errors.append("rule 必须是映射")
        rule_raw = {}
    unknown_rule = set(rule_raw) - {"t_high", "t_low", "keyword_bonus"}
    if unknown_rule:
        errors.append(f"rule 未知字段: {sorted(unknown_rule)}")
    t_high = _check_number(rule_raw.get("t_high", 0.6), 0.0, 1.0,
                           "rule.t_high", errors)
    t_low = _check_number(rule_raw.get("t_low", 0.3), 0.0, 1.0,
                          "rule.t_low", errors)
    keyword_bonus = _check_number(rule_raw.get("keyword_bonus", 0.1), 0.0, 1.0,
                                  "rule.keyword_bonus", errors)
    if t_low >= t_high:
        errors.append(f"rule.t_low 必须 < rule.t_high（当前 {t_low} >= {t_high}）")

    if errors:
        numbered = "\n".join(f"  [{i + 1}] {e}" for i, e in enumerate(errors))
        raise ValueError(f"RAG 配置校验失败（共 {len(errors)} 项）:\n{numbered}")

    return {"recall_paths": paths_out, "filters": filters_out,
            "fusion": fusion_out, "reranker": reranker_out,
            "rule": {"t_high": t_high, "t_low": t_low,
                     "keyword_bonus": keyword_bonus}}


# ---------------------------------------------------------------------------
# Knowledge-base recall paths (KeywordRecallPath.search_func injection)
# ---------------------------------------------------------------------------

def _keyword_score(tokens: List[str], title: str, content: str) -> float:
    """Explainable hit scoring: title hits weigh ×2; all tokens hitting
    the title ≈ 0.67, only-body hits ≈ 0.33 — naturally aligned with the
    default gating thresholds (0.6 / 0.3)."""
    if not tokens:
        return 0.0
    title_hits = sum(1 for w in tokens if w in (title or ""))
    content_hits = sum(1 for w in tokens if w in (content or ""))
    return round(min(1.0, (title_hits * 2 + content_hits)
                     / (len(tokens) * 3)), 4)


def _split_tags(tags: Any) -> List[str]:
    return [t.strip() for t in re.split(r"[,，]", str(tags or "")) if t.strip()]


def _kb_cs_search_func(scope: str):
    """CS-knowledge path: candidate pool (enabled entries, latest 500) →
    per-entry hit scoring → top_k.

    Deliberately not search_cs's multi-word AND retrieval — a recall path
    wants "recall" (dropping a whole entry because a natural query mixed
    in one non-hitting word is too brittle); precision is the gating
    thresholds' job. tags map to business keywords (what
    ClarifyRouteRule's keyword_bonus boost applies to).
    """
    def _search(query: str, top_k: int = 5, **_: Any) -> List[Dict[str, Any]]:
        rows = get_knowledge_store().list_cs(
            scope, include_disabled=False, limit=500)
        tokens = _cut_query(query) if query else []
        scored = []
        for row in rows:
            score = _keyword_score(tokens, row.get("title") or "",
                                   row.get("content") or "")
            if score <= 0:
                continue
            scored.append({
                "id": f"cs:{row['id']}",
                "content": f"{row.get('title') or ''}\n{row.get('content') or ''}",
                "score": score,
                "metadata": {"keywords": _split_tags(row.get("tags")),
                             "scope": scope},
            })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]
    return _search


def _kb_products_search_func(scope: str):
    """Product-knowledge path: candidate pool (latest 50 products, the
    store's retrieval cap) → hit scoring → top_k."""
    def _search(query: str, top_k: int = 5, **_: Any) -> List[Dict[str, Any]]:
        rows = get_knowledge_store().search_products(scope, limit=50)
        tokens = _cut_query(query) if query else []
        scored = []
        for row in rows:
            score = _keyword_score(tokens, row.get("goods_name") or "",
                                   row.get("extracted_content") or "")
            if score <= 0:
                continue
            scored.append({
                "id": f"goods:{row['goods_id']}",
                "content": (f"{row.get('goods_name') or ''}\n"
                            f"{row.get('extracted_content') or ''}"),
                "score": score,
                "metadata": {"keywords": [row.get("goods_name") or ""],
                             "scope": scope},
            })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]
    return _search


_SEARCH_FUNCS = {"kb_cs": _kb_cs_search_func,
                 "kb_products": _kb_products_search_func}


# ---------------------------------------------------------------------------
# Assembly + registration
# ---------------------------------------------------------------------------

def build_clarify_stage(cfg: Dict[str, Any]) -> ClarifyStage:
    """Declarative config → ClarifyStage (the caller must normalize
    first)."""
    recall_paths = [
        KeywordRecallPath(
            name=p["name"], weight=p["weight"], top_k=p["top_k"],
            search_func=_SEARCH_FUNCS[p["type"]](p["scope"]),
        )
        for p in cfg["recall_paths"]
    ]
    filters: List[Any] = []
    for flt in cfg["filters"]:
        if flt["type"] == "dedup":
            filters.append(DedupFilter(by=flt["by"]))
        elif flt["type"] == "score_threshold":
            filters.append(ScoreThresholdFilter(threshold=flt["threshold"]))
        else:
            filters.append(MaxResultsFilter(max_results=flt["max_results"]))

    fusion_cfg = cfg["fusion"]
    if fusion_cfg["type"] == "rrf":
        fusion = ReciprocalRankFusion(k=fusion_cfg["k"])
    elif fusion_cfg["type"] == "round_robin":
        fusion = RoundRobinFusion()
    else:
        fusion = WeightedScoreFusion()

    reranker_cfg = cfg["reranker"]
    if reranker_cfg["type"] == "diversity":
        reranker = DiversityReranker(lambda_param=reranker_cfg["lambda_param"])
    else:
        reranker = ScoreBasedReranker()

    rule_cfg = cfg["rule"]
    return ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=recall_paths, filters=filters,
            fusion=fusion, reranker=reranker,
        ),
        rule=ClarifyRouteRule(
            t_high=rule_cfg["t_high"], t_low=rule_cfg["t_low"],
            keyword_bonus=rule_cfg["keyword_bonus"],
        ),
    )


def apply_rag_config(cfg: Dict[str, Any]) -> List[str]:
    """Re-register the RAG assembly and return the registered stage codes.

    Three registration slots (deregister clears the instance cache → the
    next turn's resolve rebuilds):
    - ``rag_clarify``        for explicit declarations (referencable
                             directly from pattern yml / node stages);
    - ``clarify_default``    overrides the atoms builtin default assembly
                             (whose recall paths are empty);
    - stage_factory clarify  the builtin:clarify fallback factory,
                             refreshed in sync.
    """
    stage = build_clarify_stage(cfg)
    for code in ("rag_clarify", "clarify_default"):
        plugin_registry.deregister("stage", code)
        plugin_registry.register("stage", code, lambda s=stage: s)
    plugin_registry.deregister("stage_factory", "clarify")
    plugin_registry.register("stage_factory", "clarify", lambda s=stage: s)
    logger.info("RAG 配置已应用: paths=%d filters=%d fusion=%s reranker=%s "
                "rule=(t_high=%.2f, t_low=%.2f, bonus=%.2f)",
                len(cfg["recall_paths"]), len(cfg["filters"]),
                cfg["fusion"]["type"], cfg["reranker"]["type"],
                cfg["rule"]["t_high"], cfg["rule"]["t_low"],
                cfg["rule"]["keyword_bonus"])
    return ["rag_clarify", "clarify_default", "builtin:clarify"]


def reset_rag_config() -> None:
    """Restore the atoms builtin default assembly (empty recall paths)
    and remove rag_clarify."""
    from atoms.stages import _default_clarify_stage

    plugin_registry.deregister("stage", "rag_clarify")
    plugin_registry.deregister("stage", "clarify_default")
    plugin_registry.register("stage", "clarify_default", _default_clarify_stage)
    plugin_registry.deregister("stage_factory", "clarify")
    plugin_registry.register("stage_factory", "clarify", _default_clarify_stage)
    logger.info("RAG 配置已恢复内置默认（召回通路为空）")


# ---------------------------------------------------------------------------
# File persistence + startup assembly
# ---------------------------------------------------------------------------

def load_rag_config_file() -> Optional[Dict[str, Any]]:
    """Read the config file and normalize; a missing file returns None
    (= keep builtin defaults)."""
    path = rag_config_path()
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return None
    return normalize_rag_config(raw)


def save_rag_config_file(cfg: Dict[str, Any]) -> Path:
    """Normalize then persist (writes the whole config; atomicity rides
    on yaml.safe_dump's single write)."""
    normalized = normalize_rag_config(cfg)
    path = rag_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return path


def delete_rag_config_file() -> bool:
    path = rag_config_path()
    if path.exists():
        path.unlink()
        return True
    return False


def load_and_apply_rag_config() -> Optional[Dict[str, Any]]:
    """Host assembly point: apply the file when it exists (a broken file
    logs ERROR and keeps builtin defaults — an ops config error must not
    drag down the dialogue service, but must stay visible)."""
    try:
        cfg = load_rag_config_file()
    except Exception:
        logger.exception("RAG 配置文件解析失败（%s），保留内置默认",
                         rag_config_path())
        return None
    if cfg is None:
        return None
    apply_rag_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Offline dry run (recall + gating, zero LLM)
# ---------------------------------------------------------------------------

async def test_run_rag(
    cfg: Dict[str, Any], query: str,
    topic: str = "", keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Dry-run "recall + fusion/filter/rerank + gating" once per the
    config.

    Same technique as ClarifyStage._do_recall: temporarily swap the query
    into ctx.user_query, run the recaller, read pre_recall_results.
    Besides each path's raw results, returns the final gating mode and
    the boosted top score (the generation step is the only LLM call, not
    part of the dry run).
    """
    stage = build_clarify_stage(cfg)
    recaller = stage.recaller
    recaller.phase = "pre"

    ctx = DialogueContext(session_id="__rag_test__", user_query=query)
    await recaller.execute(ctx)
    fused: List[Dict[str, Any]] = list(ctx.pre_recall_results)

    # Per-path raw recall (transparency: ops sees what each path recalled
    # on its own)
    per_path: List[Dict[str, Any]] = []
    tokens = _cut_query(query) if query.strip() else []
    for path in recaller.recall_paths:
        raw = await path.recall(query, ctx)
        per_path.append({
            "name": path.name, "weight": path.weight, "top_k": path.top_k,
            "count": len(raw),
            "results": [{"id": r.get("id"), "score": r.get("score"),
                         "content": (r.get("content") or "")[:160]}
                        for r in raw],
        })

    keyword_list = [str(k) for k in (keywords or []) if str(k).strip()]
    mode, adjusted = stage.rule.route(fused, topic, keyword_list)
    return {
        "query": query,
        "tokens": tokens,
        "per_path": per_path,
        "results": [
            {"id": r.get("id"), "content": (r.get("content") or "")[:200],
             "score": r.get("score"), "source": r.get("source"),
             "keywords": (r.get("metadata") or {}).get("keywords") or []}
            for r in adjusted[:10]
        ],
        "mode": mode,
        "top_score": adjusted[0].get("score") if adjusted else None,
        "rule": {"t_high": stage.rule.t_high, "t_low": stage.rule.t_low,
                 "keyword_bonus": stage.rule.keyword_bonus},
        "scoring_note": "kb 通路打分 = (标题命中词数×2 + 正文命中词数) / (查询词数×3)，上限 1.0",
    }
