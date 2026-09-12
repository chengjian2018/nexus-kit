"""rag_config unit tests: validation / assembly / registration effect / file persistence / offline test runs.

The knowledge base is stubbed onto a tmp DB; apply-style tests restore the
builtin assembly via a fixture at teardown, so the global plugin registry
is not polluted.
"""

import asyncio

import pytest

import atoms.stages  # noqa: F401 -- registers builtin stages/factories
import atoms.stages.rag_config as rc
from atoms.knowledge.store import KnowledgeStore
from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage
from nexus.registry.plugins import registry as plugin_registry


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = KnowledgeStore(str(tmp_path / "rag-kb.db"))
    monkeypatch.setattr(rc, "get_knowledge_store", lambda: s)
    yield s
    s.close()


@pytest.fixture()
def clean_registry():
    yield
    rc.reset_rag_config()


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "rag.yaml"
    monkeypatch.setenv("NEXUS_RAG_CONFIG", str(path))
    return path


# ---------------------------------------------------------------------------
# normalize: fill defaults + collect-all-errors validation
# ---------------------------------------------------------------------------

def test_normalize_fills_defaults():
    cfg = rc.normalize_rag_config({})
    assert cfg == rc.DEFAULT_CONFIG


def test_normalize_full_config():
    cfg = rc.normalize_rag_config({
        "recall_paths": [{"type": "kb_cs", "scope": "xianyu:demo"}],
        "filters": [{"type": "dedup", "by": "id"},
                    {"type": "score_threshold", "threshold": 0.2},
                    {"type": "max_results", "max_results": 3}],
        "fusion": {"type": "rrf", "k": 30},
        "reranker": {"type": "diversity", "lambda_param": 0.5},
        "rule": {"t_high": 0.8, "t_low": 0.2, "keyword_bonus": 0.2},
    })
    path = cfg["recall_paths"][0]
    assert path["name"] == "kb_cs" and path["weight"] == 1.0 and path["top_k"] == 5
    assert cfg["fusion"] == {"type": "rrf", "k": 30}
    assert cfg["reranker"] == {"type": "diversity", "lambda_param": 0.5}


def test_normalize_collects_all_errors():
    with pytest.raises(ValueError) as e:
        rc.normalize_rag_config({
            "recall_paths": [{"type": "es", "scope": "bad scope"},
                             {"type": "kb_cs", "scope": "xianyu:d", "name": "a"},
                             {"type": "kb_cs", "scope": "xianyu:d", "name": "a"}],
            "no_such_key": 1,
            "rule": {"t_high": 0.2, "t_low": 0.8},
        })
    msg = str(e.value)
    assert "共 4 项" in msg
    assert "es" in msg and "v1 未开放" in msg
    assert "name 重复" in msg
    assert "未知顶层字段" in msg
    assert "t_low 必须 < rule.t_high" in msg


def test_normalize_rejects_none_reranker():
    with pytest.raises(ValueError, match="无重排选项不存在"):
        rc.normalize_rag_config({"reranker": {"type": "none"}})


# ---------------------------------------------------------------------------
# Assembly + registration effect
# ---------------------------------------------------------------------------

def test_build_clarify_stage_components(store):
    cfg = rc.normalize_rag_config({
        "recall_paths": [{"type": "kb_cs", "scope": "s:x"},
                         {"type": "kb_products", "scope": "s:x"}],
        "filters": [{"type": "dedup"}, {"type": "score_threshold", "threshold": 0.05}],
        "fusion": {"type": "round_robin"},
        "reranker": {"type": "diversity", "lambda_param": 0.9},
        "rule": {"t_high": 0.7, "t_low": 0.35, "keyword_bonus": 0.05},
    })
    stage = rc.build_clarify_stage(cfg)
    assert isinstance(stage, ClarifyStage)
    assert len(stage.recaller.recall_paths) == 2
    assert stage.rule.t_high == 0.7 and stage.rule.keyword_bonus == 0.05


def test_apply_then_reset_registry(store, clean_registry):
    cfg = rc.normalize_rag_config(
        {"recall_paths": [{"type": "kb_cs", "scope": "s:x"}],
         "rule": {"t_high": 0.9, "t_low": 0.1}})
    codes = rc.apply_rag_config(cfg)
    assert set(codes) == {"rag_clarify", "clarify_default", "builtin:clarify"}
    applied = plugin_registry.resolve("stage", "rag_clarify")
    assert isinstance(applied, ClarifyStage)
    assert isinstance(applied.rule, ClarifyRouteRule)
    assert applied.rule.t_high == 0.9  # the newly assembled config takes effect (instance cache cleared by deregister)

    rc.reset_rag_config()
    assert not plugin_registry.has("stage", "rag_clarify")
    restored = plugin_registry.resolve("stage", "clarify_default")
    assert isinstance(restored, ClarifyStage)
    assert restored.recaller.recall_paths == []  # builtin default: recall paths empty


# ---------------------------------------------------------------------------
# File persistence + startup assembly
# ---------------------------------------------------------------------------

def test_save_load_delete_file(config_file):
    assert rc.load_rag_config_file() is None  # missing file = keep the builtin config
    rc.save_rag_config_file({"rule": {"t_high": 0.75}})
    loaded = rc.load_rag_config_file()
    assert loaded["rule"]["t_high"] == 0.75
    assert loaded["fusion"] == {"type": "weighted"}  # defaults were filled in before persisting
    assert rc.delete_rag_config_file() is True
    assert rc.load_rag_config_file() is None


def test_load_and_apply_bad_file_keeps_builtin(config_file, clean_registry):
    config_file.write_text("recall_paths: [oops]\n", encoding="utf-8")
    assert rc.load_and_apply_rag_config() is None
    restored = plugin_registry.resolve("stage", "clarify_default")
    assert restored.recaller.recall_paths == []  # builtin assembly left intact


def test_load_and_apply_applies_file(config_file, clean_registry):
    rc.save_rag_config_file(
        {"recall_paths": [{"type": "kb_cs", "scope": "s:x"}],
         "rule": {"t_high": 0.8}})
    cfg = rc.load_and_apply_rag_config()
    assert cfg is not None and cfg["rule"]["t_high"] == 0.8
    assert plugin_registry.resolve("stage", "rag_clarify").rule.t_high == 0.8


# ---------------------------------------------------------------------------
# Offline test runs (recall + gating, zero LLM)
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_store(store):
    store.add_cs("s:x", "退货政策", "自签收起 7 天内支持无理由退货", tags="售后,退货")
    store.add_cs("s:x", "发货时效", "付款后 48 小时内发货", tags="物流")
    store.add_cs("s:x", "停用条目", "不应被召回", tags="售后")
    # list_cs sorts by updated_at, so locate the row by title instead of index
    disabled_id = next(r["id"] for r in store.list_cs("s:x")
                       if r["title"] == "停用条目")
    store.update_cs(disabled_id, {"enabled": False})
    store.upsert_product("s:x", 1001, "iPhone 13 128G",
                         extracted_content="国行在保 电池89%")
    return store


def _cfg():
    return rc.normalize_rag_config({
        "recall_paths": [
            {"type": "kb_cs", "scope": "s:x", "top_k": 5},
            {"type": "kb_products", "scope": "s:x", "top_k": 3, "weight": 0.6},
        ],
        "filters": [{"type": "score_threshold", "threshold": 0.1}],
    })


def test_run_kb_mode_on_strong_match(seeded_store):
    out = asyncio.run(rc.test_run_rag(_cfg(), "退货政策 怎么算", topic="退货"))
    assert out["mode"] == "kb"
    assert out["top_score"] >= 0.6
    assert out["results"][0]["id"].startswith("cs:")
    assert "退货" in out["tokens"]


def test_run_keyword_bonus_adds_score(seeded_store):
    plain = asyncio.run(rc.test_run_rag(_cfg(), "退货 政策"))
    bonused = asyncio.run(rc.test_run_rag(_cfg(), "退货 政策", topic="退货"))
    assert bonused["top_score"] == pytest.approx(plain["top_score"] + 0.1)


def test_run_fallback_on_irrelevant(seeded_store):
    out = asyncio.run(rc.test_run_rag(_cfg(), "量子涨落问题"))
    assert out["mode"] == "fallback"
    assert out["results"] == [] and out["top_score"] is None


def test_run_excludes_disabled_cs(seeded_store):
    out = asyncio.run(rc.test_run_rag(_cfg(), "停用 不应被召回"))
    assert all("不应被召回" not in (r["content"] or "")
               for r in out["results"])


def test_run_reports_per_path(seeded_store):
    out = asyncio.run(rc.test_run_rag(_cfg(), "iPhone 国行"))
    names = {p["name"]: p["count"] for p in out["per_path"]}
    assert names["kb_products"] >= 1
    assert set(names) == {"kb_cs", "kb_products"}
