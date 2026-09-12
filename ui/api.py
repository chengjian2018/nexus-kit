"""Ops-console API — /api/v1/console/*（PRD P0 面，见 docs/design/ops-console-prd.md §8）。

只读 pattern 视图 + 目录查询 + 知识库管理 CRUD / 试搜台。约定：

- 响应沿用 host 的 ``{code, message, status, data}`` 包裹（code "0" 成功）；
- 路径挂在 /api/v1/ 下，天然被 host.main 的 NEXUS_API_KEY 中间件覆盖
  （channel 除外的那条规则不影响 console）；
- 端点全部同步 def（FastAPI 丢线程池执行）——知识库是 sqlite3 同步连接
  （自带锁），pattern 序列化也是纯 CPU；
- 事实源口径：pattern 一律来自注册表（当前全部 code-managed，PRD D-1
  的 fork-to-edit 属 P1）；知识库写操作经 atoms.knowledge.store 的管理
  CRUD（update 语义 = 出现的键才更新，None 显式清空）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from atoms.knowledge.store import _cut_query, get_knowledge_store
from atoms.stages import rag_config
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
    """首次请求前暖注册表（真实服务里 host.main import 时已做过；独立
    挂载/测试场景懒触发）。discover 幂等：同名同 factory 是 no-op。"""
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


def _match_explanation(
    row: Dict[str, Any], tokens: List[str], fields: List[str]
) -> Dict[str, List[str]]:
    """逐字段解释命中：该字段文本包含了哪些分词（试搜台展示用）。"""
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
    """Pattern 概要（两层模型：pattern_type / nodes / plugins / allow_toolset）。"""
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
    """PUT 语义：缺席 = 不修改；显式 null = 清空该字段（store.update_product）。"""
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
    """试搜台：跑生产同款检索（search_products / search_cs），并附分词与
    逐字段命中解释——让运营对「关键词 LIKE 检索」建立真实预期。"""
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
# RAG retrieval config (clarify recall pipeline; see the atoms/stages/rag_config.py module docstring)
# ---------------------------------------------------------------------------

def _current_rag_config() -> Tuple[Dict[str, Any], str, Optional[str]]:
    """(当前配置, 来源, 文件错误)。坏文件降级 builtin 并带上错误说明。"""
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
    """离线试跑：召回 + 门控零 LLM，确定性呈现配置效果（改参数→看模式变化）。"""
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
# Static asset location (used by host.main's /console mount)
# ---------------------------------------------------------------------------

def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"
