"""Ops-console API — /api/v1/console/* (PRD P0 surface; see
docs/design/ops-console-prd.md §8).

Read-only pattern views + catalog queries + knowledge-base management CRUD /
search trial console. Conventions:

- Responses reuse host's ``{code, message, status, data}`` envelope
  (code "0" means success);
- Routes are mounted under /api/v1/, so host.main's NEXUS_API_KEY middleware
  covers them naturally (the channel-exemption rule does not affect the
  console);
- Endpoints are sync def (FastAPI dispatches them to the threadpool) — the
  knowledge base is a synchronous sqlite3 connection (with its own lock) and
  pattern serialization is pure CPU too; the exception is the session-review
  section (§Session review), which goes through the aiosqlite-based
  SessionStore and is therefore async def;
- Source of truth: patterns always come from the registry (code-managed
  plus ui/studio's console-hosted copies — the same-code console version
  wins, D-1's fork-to-edit); knowledge-base writes go
  through atoms.knowledge.store's management CRUD (update semantics = only
  the keys that appear are updated; None explicitly clears a field).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from atoms.knowledge.store import _cut_query, get_knowledge_store
from atoms.stages import rag_config
from nexus.context import decode_tool_call_content
from nexus.model.serialization import pattern_to_dict, pattern_to_yaml
from nexus.registry.patterns import (
    discover_builtin_patterns,
    registry as pattern_registry,
)
from nexus.registry.plugins import (
    discover_builtin_plugins,
    registry as plugin_registry,
)
from nexus.registry.tools import (
    discover_builtin_tools,
    registry as tool_registry,
)
from nexus.visualize import pattern_to_mermaid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/console")

_discovered = False

# Plugin kinds passed straight through by catalog/{kind} (PRD §4.1: reference codes are always dropdown-selected, never hand-typed)
_CATALOG_KINDS = ("stage", "executor", "messages_builder")

_SCOPE_RE = re.compile(r"^[^:\s]+:[^:\s]+$")


def _ensure_discovery() -> None:
    """Warm the registries before the first request (host.main's import
    already did it in the real service; lazily triggered in standalone
    mounting / tests). Discovery is idempotent: same name + same factory is
    a no-op."""
    global _discovered
    if _discovered:
        return
    discover_builtin_tools()
    discover_builtin_patterns()
    discover_builtin_plugins()
    _discovered = True


def _ok(data: Any = None, message: str = "success") -> Dict[str, Any]:
    return {"code": "0", "status": True, "message": message,
            "data": data if data is not None else {}}


def _fail(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "status": False, "message": message},
    )


def _bad_scope(scope: str) -> Optional[JSONResponse]:
    if not _SCOPE_RE.match(scope or ""):
        return _fail(400, "400",
                     "scope 格式必须为 {channel}:{account_id}（如 xianyu:demo）")
    return None


def _owned_collection(
    store: Any, collection_id: int, scope: str
) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
    """Scope-ownership guard for id-addressed endpoints — the same isolation
    statement as the list layer: scope is required and format-checked;
    a mismatching owner is reported as 404 (never leaking other scopes'
    existence). Returns (collection, None) or (None, error_response)."""
    bad = _bad_scope(scope or "")
    if bad is not None:
        return None, bad
    coll = store.get_collection(collection_id)
    if coll is None or coll["scope"] != scope:
        return None, _fail(404, "404", f"知识库不存在: id={collection_id}")
    return coll, None


def _match_explanation(
    row: Dict[str, Any], tokens: List[str], fields: List[str]
) -> Dict[str, List[str]]:
    """Per-field hit explanation: which tokens each field's text contained (for the search-preview display)."""
    explanation: Dict[str, List[str]] = {}
    for field in fields:
        text = str(row.get(field) or "")
        hits = [w for w in tokens if w in text]
        if hits:
            explanation[field] = hits
    return explanation


# ---------------------------------------------------------------------------
# Pattern read-only views (PRD §6.2 / §6.3 — no edit state in P0)
# ---------------------------------------------------------------------------

def _pattern_meta(pattern: Any) -> Dict[str, Any]:
    """Pattern summary (two-layer model: pattern_type / nodes / plugins / allow_toolset)."""
    skeleton = [slot for entry_dict in (pattern.stages or [])
                for slot in entry_dict.keys()]
    meta: Dict[str, Any] = {
        "code": pattern.code,
        "name": pattern.name,
        "description": pattern.description,
        "pattern_type": getattr(pattern, "pattern_type", "agent"),
        "entry_node_code": pattern.entry_node_code,
        "node_count": len(pattern.nodes or []),
        "plugins": dict(pattern.plugins or {}),
        "allow_toolset": list(pattern.allow_toolset or []),
        "skeleton": skeleton,
        "managed": "code",
    }
    if meta["pattern_type"] == "agent":
        meta["max_steps"] = pattern.max_steps
    return meta


@router.get("/patterns")
def list_patterns() -> Dict[str, Any]:
    _ensure_discovery()
    patterns = [_pattern_meta(p) for p in pattern_registry.list_patterns()]
    return _ok({"patterns": patterns})


@router.get("/patterns/{code}")
def get_pattern_detail(code: str):
    _ensure_discovery()
    pattern = pattern_registry.get(code)
    if pattern is None:
        return _fail(404, "404",
                     f"pattern '{code}' 未注册，已注册: "
                     f"{pattern_registry.list_codes()}")
    try:
        return _ok({
            "meta": _pattern_meta(pattern),
            "yaml": pattern_to_yaml(pattern),
            "mermaid": pattern_to_mermaid(pattern),
            "tree": pattern_to_dict(pattern),
        })
    except Exception:
        logger.exception("pattern 序列化失败: %s", code)
        return _fail(500, "500", f"pattern '{code}' 序列化失败，详情见服务日志")


# ---------------------------------------------------------------------------
# Catalog (data source for the reference-code dropdowns)
# ---------------------------------------------------------------------------

@router.get("/catalog/tools")
def catalog_tools() -> Dict[str, Any]:
    _ensure_discovery()
    return _ok({"toolsets": tool_registry.get_available_toolsets()})


@router.get("/catalog/{kind}")
def catalog(kind: str):
    _ensure_discovery()
    if kind not in _CATALOG_KINDS:
        return _fail(404, "404",
                     f"未知目录 kind: {kind!r}（合法: {list(_CATALOG_KINDS)}）")
    return _ok({"kind": kind, "codes": plugin_registry.list_codes(kind)})


# ---------------------------------------------------------------------------
# Knowledge base (PRD §6.4)
# ---------------------------------------------------------------------------

class ProductIn(BaseModel):
    scope: str = Field(max_length=128)
    goods_id: int
    goods_name: str = Field(min_length=1, max_length=512)
    price: Optional[str] = Field(default=None, max_length=64)
    sold_quantity: Optional[int] = None
    specifications: Optional[str] = None
    extracted_content: Optional[str] = None


class ProductPatch(BaseModel):
    """PUT semantics: absent = leave unchanged; explicit null = clear the
    field (store.update_product)."""
    goods_name: Optional[str] = Field(default=None, max_length=512)
    price: Optional[str] = Field(default=None, max_length=64)
    sold_quantity: Optional[int] = None
    specifications: Optional[str] = None
    extracted_content: Optional[str] = None


class CsIn(BaseModel):
    scope: str = Field(max_length=128)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)
    tags: Optional[str] = Field(default=None, max_length=256)
    enabled: bool = True


class CsPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    content: Optional[str] = None
    tags: Optional[str] = Field(default=None, max_length=256)
    enabled: Optional[bool] = None


class SearchTestIn(BaseModel):
    scope: str = Field(max_length=128)
    query: str = Field(default="", max_length=4000)
    goods_id: Optional[int] = None
    limit: int = 10


class ClearScopeIn(BaseModel):
    scope: str = Field(max_length=128)


class FieldDef(BaseModel):
    """One field definition of a custom collection (name is the record JSON's key)."""
    name: str = Field(min_length=1, max_length=64,
                      pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: Optional[str] = Field(default=None, max_length=64)
    type: str = Field(default="text", pattern=r"^(text|textarea|number)$")
    required: bool = False


class CollectionIn(BaseModel):
    scope: str = Field(max_length=128)
    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=512)
    fields: List[FieldDef] = Field(min_length=1, max_length=50)


class CollectionPatch(BaseModel):
    """PUT semantics match the builtin collections: absent = untouched; a
    fields change recomputes the records' search text and prunes removed
    fields' values (store.update_collection)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=512)
    fields: Optional[List[FieldDef]] = None


class RecordIn(BaseModel):
    data: Dict[str, Any]


class RagConfigIn(BaseModel):
    config: Dict[str, Any]


class RagTestRunIn(BaseModel):
    config: Optional[Dict[str, Any]] = None  # absent = the currently saved config
    query: str = Field(min_length=1, max_length=4000)
    topic: str = Field(default="", max_length=256)
    keywords: List[str] = Field(default_factory=list)


@router.get("/knowledge/scopes")
def knowledge_scopes() -> Dict[str, Any]:
    return _ok({"scopes": get_knowledge_store().list_scopes()})


@router.get("/knowledge/products")
def knowledge_products(
    scope: str, query: str = "", goods_id: Optional[int] = None,
    limit: int = 20,
):
    bad = _bad_scope(scope)
    if bad is not None:
        return bad
    limit = max(1, min(limit, 50))  # matches the store's retrieval hard cap
    rows = get_knowledge_store().search_products(
        scope, query=query or None, goods_id=goods_id, limit=limit)
    return _ok({"scope": scope, "rows": rows, "limit": limit})


@router.post("/knowledge/products")
def knowledge_product_create(body: ProductIn):
    bad = _bad_scope(body.scope)
    if bad is not None:
        return bad
    get_knowledge_store().upsert_product(
        body.scope, body.goods_id, body.goods_name,
        price=body.price, sold_quantity=body.sold_quantity,
        specifications=body.specifications,
        extracted_content=body.extracted_content)
    return _ok({"scope": body.scope, "goods_id": body.goods_id},
               message="已保存（同 scope + goods_id 存在则更新）")


@router.put("/knowledge/products")
def knowledge_product_update(scope: str, goods_id: int, body: ProductPatch):
    bad = _bad_scope(scope)
    if bad is not None:
        return bad
    fields = body.model_dump(exclude_unset=True)
    try:
        exists = get_knowledge_store().update_product(scope, goods_id, fields)
    except ValueError as e:
        return _fail(400, "400", str(e))
    if not exists:
        return _fail(404, "404", f"商品知识不存在: scope={scope} goods_id={goods_id}")
    return _ok({"scope": scope, "goods_id": goods_id, "updated_fields": sorted(fields)})


@router.delete("/knowledge/products")
def knowledge_product_delete(scope: str, goods_id: int):
    bad = _bad_scope(scope)
    if bad is not None:
        return bad
    if not get_knowledge_store().delete_product(scope, goods_id):
        return _fail(404, "404", f"商品知识不存在: scope={scope} goods_id={goods_id}")
    return _ok({"scope": scope, "goods_id": goods_id}, message="已删除")


@router.get("/knowledge/cs-entries")
def knowledge_cs_entries(
    scope: str, include_disabled: bool = True,
    limit: int = 100, offset: int = 0,
):
    bad = _bad_scope(scope)
    if bad is not None:
        return bad
    rows = get_knowledge_store().list_cs(
        scope, include_disabled=include_disabled, limit=limit, offset=offset)
    return _ok({"scope": scope, "rows": rows})


@router.post("/knowledge/cs-entries")
def knowledge_cs_create(body: CsIn):
    bad = _bad_scope(body.scope)
    if bad is not None:
        return bad
    entry_id = get_knowledge_store().add_cs(
        body.scope, body.title, body.content, tags=body.tags,
        enabled=body.enabled)
    return _ok({"id": entry_id, "scope": body.scope}, message="已新增")


@router.put("/knowledge/cs-entries/{entry_id}")
def knowledge_cs_update(entry_id: int, body: CsPatch):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return _fail(400, "400", "没有可更新的字段")
    # title/content are NOT NULL columns: explicit null/empty string = illegal clearing
    for required in ("title", "content"):
        if required in fields and not fields[required]:
            return _fail(400, "400", f"{required} 不能为空")
    try:
        exists = get_knowledge_store().update_cs(entry_id, fields)
    except ValueError as e:
        return _fail(400, "400", str(e))
    if not exists:
        return _fail(404, "404", f"客服知识不存在: id={entry_id}")
    return _ok({"id": entry_id, "updated_fields": sorted(fields)})


@router.delete("/knowledge/cs-entries/{entry_id}")
def knowledge_cs_delete(entry_id: int):
    if not get_knowledge_store().delete_cs(entry_id):
        return _fail(404, "404", f"客服知识不存在: id={entry_id}")
    return _ok({"id": entry_id}, message="已删除")


@router.post("/knowledge/search-test")
def knowledge_search_test(body: SearchTestIn):
    """Search preview: runs the production-identical retrieval
    (search_products / search_cs) with tokens and per-field hit explanations
    attached — lets operators build realistic expectations of "keyword LIKE
    search"."""
    bad = _bad_scope(body.scope)
    if bad is not None:
        return bad
    store = get_knowledge_store()
    tokens = (_cut_query(body.query)
              if body.query.strip() and body.goods_id is None else [])
    products = store.search_products(
        body.scope, query=body.query or None, goods_id=body.goods_id,
        limit=body.limit)
    cs_entries = store.search_cs(
        body.scope, query=body.query or None, limit=body.limit)
    return _ok({
        "scope": body.scope,
        "query": body.query,
        "tokens": tokens,
        "match": "多词 AND（词间同时满足）；单词内字段间 OR"
                 "（goods_name/extracted_content、title/content 命中任一即可）",
        "products": [
            {**row,
             "_match": _match_explanation(
                 row, tokens, ["goods_name", "extracted_content"])}
            for row in products
        ],
        "cs_entries": [
            {**row,
             "_match": _match_explanation(row, tokens, ["title", "content"])}
            for row in cs_entries
        ],
    })


@router.post("/knowledge/clear-scope")
def knowledge_clear_scope(body: ClearScopeIn):
    bad = _bad_scope(body.scope)
    if bad is not None:
        return bad
    counts = get_knowledge_store().clear_scope(body.scope)
    return _ok(counts, message=f"空间 {body.scope} 已清空")


# ---------------------------------------------------------------------------
# Custom knowledge collections (ops-console: user-defined tables; scope
# isolated like the built-ins, console-managed only — not in the agent
# toolset, so the search sandbox is unaffected)
# ---------------------------------------------------------------------------

@router.get("/knowledge/collections")
def knowledge_collections(scope: str):
    bad = _bad_scope(scope)
    if bad is not None:
        return bad
    return _ok({"scope": scope,
                "collections": get_knowledge_store().list_collections(scope)})


@router.post("/knowledge/collections")
def knowledge_collection_create(body: CollectionIn):
    bad = _bad_scope(body.scope)
    if bad is not None:
        return bad
    try:
        cid = get_knowledge_store().create_collection(
            body.scope, body.name, body.description,
            [f.model_dump() for f in body.fields])
    except ValueError as e:
        return _fail(400, "400", str(e))
    return _ok({"id": cid, "scope": body.scope}, message="知识库已创建")


@router.get("/knowledge/collections/{collection_id}")
def knowledge_collection_get(collection_id: int, scope: str):
    coll, err = _owned_collection(get_knowledge_store(), collection_id, scope)
    if err is not None:
        return err
    return _ok({"collection": coll})


@router.put("/knowledge/collections/{collection_id}")
def knowledge_collection_update(collection_id: int, scope: str,
                                body: CollectionPatch):
    coll, err = _owned_collection(get_knowledge_store(), collection_id, scope)
    if err is not None:
        return err
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return _fail(400, "400", "没有可更新的字段")
    try:
        exists = get_knowledge_store().update_collection(collection_id, patch)
    except ValueError as e:
        return _fail(400, "400", str(e))
    if not exists:
        return _fail(404, "404", f"知识库不存在: id={collection_id}")
    return _ok({"id": collection_id, "updated_fields": sorted(patch)})


@router.delete("/knowledge/collections/{collection_id}")
def knowledge_collection_delete(collection_id: int, scope: str):
    coll, err = _owned_collection(get_knowledge_store(), collection_id, scope)
    if err is not None:
        return err
    get_knowledge_store().delete_collection(collection_id)
    return _ok({"id": collection_id}, message="已删除（含全部记录）")


@router.get("/knowledge/collections/{collection_id}/records")
def knowledge_collection_records(
    collection_id: int, scope: str, query: str = "", limit: int = 100,
    offset: int = 0,
):
    """Record list (the response carries the collection's field definitions — the frontend renders dynamic columns from them)."""
    store = get_knowledge_store()
    coll, err = _owned_collection(store, collection_id, scope)
    if err is not None:
        return err
    limit = max(1, min(limit, 200))
    rows = store.list_records(collection_id, query=query or None,
                              limit=limit, offset=offset)
    return _ok({"collection": coll, "rows": rows, "limit": limit})


@router.post("/knowledge/collections/{collection_id}/records")
def knowledge_record_create(collection_id: int, scope: str, body: RecordIn):
    store = get_knowledge_store()
    _, err = _owned_collection(store, collection_id, scope)
    if err is not None:
        return err
    try:
        rid = store.add_record(collection_id, body.data)
    except ValueError as e:
        return _fail(400, "400", str(e))
    return _ok({"id": rid}, message="已新增")


@router.put("/knowledge/collections/{collection_id}/records/{record_id}")
def knowledge_record_update(collection_id: int, scope: str, record_id: int,
                            body: RecordIn):
    """Whole-row replace of the record (the form always submits every field; validation uses the current field definitions)."""
    store = get_knowledge_store()
    _, err = _owned_collection(store, collection_id, scope)
    if err is not None:
        return err
    try:
        exists = store.update_record(collection_id, record_id, body.data)
    except ValueError as e:
        return _fail(400, "400", str(e))
    if not exists:
        return _fail(404, "404",
                     f"记录不存在: id={record_id}（collection={collection_id}）")
    return _ok({"id": record_id}, message="已保存")


@router.delete("/knowledge/collections/{collection_id}/records/{record_id}")
def knowledge_record_delete(collection_id: int, scope: str, record_id: int):
    store = get_knowledge_store()
    _, err = _owned_collection(store, collection_id, scope)
    if err is not None:
        return err
    if not store.delete_record(collection_id, record_id):
        return _fail(404, "404",
                     f"记录不存在: id={record_id}（collection={collection_id}）")
    return _ok({"id": record_id}, message="已删除")


# ---------------------------------------------------------------------------
# RAG retrieval config (clarify recall pipeline; see the atoms/stages/rag_config.py module docstring)
# ---------------------------------------------------------------------------

def _current_rag_config() -> Tuple[Dict[str, Any], str, Optional[str]]:
    """(current config, source, file error). A broken file degrades to the
    builtin default together with an error description."""
    try:
        cfg = rag_config.load_rag_config_file()
    except ValueError as e:
        import copy
        return copy.deepcopy(rag_config.DEFAULT_CONFIG), "builtin", str(e)
    if cfg is not None:
        return cfg, "file", None
    import copy
    return copy.deepcopy(rag_config.DEFAULT_CONFIG), "builtin", None


@router.get("/rag/config")
def rag_get_config() -> Dict[str, Any]:
    cfg, source, file_error = _current_rag_config()
    data: Dict[str, Any] = {
        "config": cfg, "source": source,
        "path": str(rag_config.rag_config_path()),
        "supported": {
            "recall_path_types": list(rag_config._PATH_TYPES),
            "filter_types": list(rag_config._FILTER_TYPES),
            "fusion_types": list(rag_config._FUSION_TYPES),
            "reranker_types": list(rag_config._RERANKER_TYPES),
        },
    }
    if file_error:
        data["file_error"] = file_error
    return _ok(data)


@router.put("/rag/config")
def rag_put_config(body: RagConfigIn):
    try:
        normalized = rag_config.normalize_rag_config(body.config)
        rag_config.build_clarify_stage(normalized)  # assembly dry-run
    except ValueError as e:
        return _fail(400, "400", str(e))
    try:
        path = rag_config.save_rag_config_file(normalized)
    except OSError as e:
        return _fail(500, "500", f"配置写入失败（{rag_config.rag_config_path()}）: {e}")
    codes = rag_config.apply_rag_config(normalized)
    return _ok({"path": str(path), "applied_codes": codes},
               message="已保存并生效（下一对话轮起；影响声明 clarify 槽的 pattern）")


@router.post("/rag/reset")
def rag_reset():
    existed = rag_config.delete_rag_config_file()
    rag_config.reset_rag_config()
    return _ok({"file_deleted": existed},
               message="已恢复内置默认装配（召回通路为空）")


@router.post("/rag/test-run")
def rag_test_run(body: RagTestRunIn):
    """Offline dry run: recall + gating with zero LLM, deterministically showing the config's effect (change a param → see the pattern change)."""
    if body.config is not None:
        try:
            cfg = rag_config.normalize_rag_config(body.config)
        except ValueError as e:
            return _fail(400, "400", str(e))
    else:
        cfg, _, file_error = _current_rag_config()
        if file_error:
            return _fail(400, "400", f"当前配置文件非法，试跑请显式传入 config: {file_error}")
    import asyncio

    result = asyncio.run(rag_config.test_run_rag(
        cfg, body.query, topic=body.topic, keywords=body.keywords))
    return _ok(result)


# ---------------------------------------------------------------------------
# Session review (read-only audit) — list / detail / merged timeline
#
# A read-only audit surface: all data comes from SessionStore (the sessions /
# messages / trace_events tables) with no write operations. SessionStore is
# aiosqlite, so these endpoints are async def. Dependencies are fetched
# lazily from host.main via _session_deps (a deferred import against a
# cycle); tests monkeypatch that function to inject a temporary store.
# ---------------------------------------------------------------------------

_TRACE_LIMIT = 1000  # per-session trace fetch cap (the store caps at the same value)


def _session_deps():
    """(store, turn_registry) — session-audit read dependencies after host.main's assembly."""
    import host.main as host_main
    return host_main.store, host_main.turn_registry


def _json_field(value: Any, default: Any) -> Any:
    """JSON TEXT column decode; a bad payload degrades to the default (the same
    policy as host.main's /api/v1/sessions: degrade, never 500)."""
    if isinstance(value, str):
        try:
            return json.loads(value) if value else default
        except ValueError:
            return default
    return value if value is not None else default


def _decode_session_row(row: Dict[str, Any]) -> Dict[str, Any]:
    for key, default in (("graph_state", {}), ("filled_slots", {}),
                         ("task_info", {})):
        if key in row:
            row[key] = _json_field(row[key], default)
    return row


@router.get("/sessions")
async def console_list_sessions(
    pattern_code: str = "", q: str = "", limit: int = 50, offset: int = 0,
) -> Dict[str, Any]:
    store, turn_registry = _session_deps()
    if store is None:
        return _fail(500, "500", "会话存储未启用")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        rows = await store.list_sessions(
            pattern_code=pattern_code or None,
            limit=limit + 1, offset=offset,
            session_id_contains=q or None,
        )
    except Exception:
        logger.exception("会话审查：查询会话列表失败")
        return _fail(500, "500", "查询会话列表失败，请稍后重试")
    has_more = len(rows) > limit
    sessions = [_decode_session_row(r) for r in rows[:limit]]
    for s in sessions:
        s["turn_running"] = turn_registry.has_running(s["session_id"])
    return _ok({"sessions": sessions, "has_more": has_more,
                "limit": limit, "offset": offset})


@router.get("/sessions/{session_id}")
async def console_session_detail(session_id: str) -> Dict[str, Any]:
    store, turn_registry = _session_deps()
    if store is None:
        return _fail(500, "500", "会话存储未启用")
    try:
        row = await store.get_session(session_id)
    except Exception:
        logger.exception("会话审查：查询会话详情失败")
        return _fail(500, "500", "查询会话详情失败，请稍后重试")
    if row is None:
        return _fail(404, "404", f"session_id '{session_id}' 不存在")
    _decode_session_row(row)
    row["turn_running"] = turn_registry.has_running(session_id)
    return _ok({"session": row})


@router.get("/sessions/{session_id}/timeline")
async def console_session_timeline(session_id: str) -> Dict[str, Any]:
    """Merged audit timeline of messages + trace_events.

    The two tables' autoincrement ids are each ordered within their own
    table with no global ordering, so the merge is an approximate
    interleave: created_at (microsecond epoch) is the primary order, with
    messages preferred at identical timestamps (item_type lexicographic
    order) as the stable tie-breaker.
    """
    store, _ = _session_deps()
    if store is None:
        return _fail(500, "500", "会话存储未启用")
    try:
        session_row = await store.get_session(session_id)
        messages = await store.get_messages(session_id)
        trace = await store.get_trace_events(session_id, limit=_TRACE_LIMIT)
    except Exception:
        logger.exception("会话审查：查询会话时间线失败")
        return _fail(500, "500", "查询会话时间线失败，请稍后重试")
    if session_row is None or messages is None or trace is None:
        return _fail(404, "404", f"session_id '{session_id}' 不存在")

    items: List[Dict[str, Any]] = []
    for m in messages:
        item = dict(m)
        item["item_type"] = "message"
        meta = item.get("metadata") or {}
        # Audit-relevant flags surfaced first: synthetic = hallucination-intercept backfill / rewritten = hook rewrite
        item["flags"] = {k: meta[k] for k in ("synthetic", "rewritten")
                         if meta.get(k)}
        if item.get("role") == "assistant":
            decoded = decode_tool_call_content(item.get("content") or "")
            if decoded is not None:
                item["content"], item["tool_calls"] = decoded
        items.append(item)
    for e in trace:
        item = dict(e)
        item["item_type"] = "trace"
        items.append(item)
    items.sort(key=lambda it: (it.get("created_at") or 0, it["item_type"]))

    turns: List[Dict[str, Any]] = []  # turns aggregated from the trace side in first-appearance order
    by_turn: Dict[str, Dict[str, Any]] = {}
    for e in trace:
        tid = e.get("turn_id") or ""
        if not tid:
            continue
        entry = by_turn.get(tid)
        if entry is None:
            entry = {"turn_id": tid, "count": 0}
            by_turn[tid] = entry
            turns.append(entry)
        entry["count"] += 1

    return _ok({
        "session": _decode_session_row(session_row),
        "items": items,
        "turns": turns,
        "trace_truncated": len(trace) >= _TRACE_LIMIT,
    })


# ---------------------------------------------------------------------------
# Static asset location (used by host.main's /console mount)
# ---------------------------------------------------------------------------

def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"
