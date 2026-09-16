"""Baidu Wenku AIPPT API client — the external-domain capabilities of the
ppt_generator_agent app, ported from the source skill scripts
(generate_ppt.py / ppt_theme_list.py / random_ppt_theme.py).

Triage: every function here is deterministic code — theme listing and PPT
generation are external API calls, theme categorization is keyword table
matching. None of them may ride a prompt (a wrong tpl_id or a fabricated
URL must be silently impossible); the app's unified stage (stages.py) calls
them directly on the transitions that need them, zero LLM in the loop.

The two API capabilities are ALSO registered in the tool registry
(toolset ``ppt_gen``) for visibility/reuse; the FSM pattern grants no
toolset (deny-by-default — the fsm stages path never runs an LLM tool
loop), so the grants stay closed.

Secrets: the API key is read from the ``BAIDU_API_KEY`` environment
variable at call time — never hardcoded, never in config.yaml.
"""

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import requests

from nexus.registry.tools import registry, tool_result, tool_error

logger = logging.getLogger(__name__)

API_BASE = "https://qianfan.baidubce.com/v2/tools/ai_ppt/"

# PPT generation is a 2-5 minute streaming job (source skill: "set timeout
# to 300 seconds"); connect fast, read long.
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 600

# Cap the theme list like the source skill (ppt_theme_list.py)
MAX_THEMES = 100


def get_api_key() -> Optional[str]:
    """The Baidu API key from the environment (None when unset)."""
    return os.getenv("BAIDU_API_KEY") or None


# ============================================================================
# Theme list — port of ppt_theme_list.py
# ============================================================================

def fetch_ppt_themes(api_key: str) -> List[Dict[str, Any]]:
    """Fetch the available PPT themes (style_name_list / style_id / tpl_id).

    Raises RuntimeError on API-reported errors, requests.RequestException on
    transport errors — the caller (stage guard) owns the honest-failure path.
    """
    url = API_BASE + "get_ppt_theme"
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "X-Appbuilder-From": "openclaw",
    }
    response = requests.post(url, headers=headers,
                             timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    result = response.json()
    if "errno" in result and result["errno"] != 0:
        raise RuntimeError(result["errmsg"])
    themes = []
    for count, theme in enumerate(result["data"]["ppt_themes"], start=1):
        if count > MAX_THEMES:
            break
        themes.append({
            "style_name_list": theme["style_name_list"],
            "style_id": theme["style_id"],
            "tpl_id": theme["tpl_id"],
        })
    return themes


# ============================================================================
# Smart categorization — port of random_ppt_theme.suggest_category_by_query
# (keyword table + deterministic fallback; ordering IS priority)
# ============================================================================

CATEGORY_KEYWORDS = [
    # Business & Corporate (highest priority for formal content)
    ("企业商务", [
        "企业", "公司", "商务", "商业", "商业计划", "商业报告",
        "营销", "市场", "销售", "财务", "会计", "审计", "投资", "融资",
        "战略", "管理", "运营", "人力资源", "hr", "董事会", "股东",
        "年报", "季报", "财报", "业绩", "kpi", "okr", "商业计划书",
        "提案", "策划", "方案", "报告", "总结", "规划", "计划",
    ]),
    # Technology & Future Tech
    ("未来科技", [
        "未来", "科技", "人工智能", "ai", "机器学习", "深度学习",
        "大数据", "云计算", "区块链", "物联网", "iot", "5g", "6g",
        "量子计算", "机器人", "自动化", "智能制造", "智慧城市",
        "虚拟现实", "vr", "增强现实", "ar", "元宇宙", "数字孪生",
        "芯片", "半导体", "集成电路", "电子", "通信", "网络",
        "网络安全", "信息安全", "数字化", "数字化转型",
        "科幻", "高科技", "前沿科技", "科技创新", "技术",
    ]),
    # Education & Children
    ("卡通手绘", [
        "卡通", "动画", "动漫", "儿童", "幼儿", "小学生", "中学生",
        "教育", "教学", "课件", "教案", "学习", "培训", "教程",
        "趣味", "有趣", "可爱", "活泼", "生动", "绘本", "漫画",
        "手绘", "插画", "图画", "图形", "游戏", "玩乐", "娱乐",
    ]),
    # Year-end & Summary
    ("年终总结", [
        "年终", "年度", "季度", "月度", "周报", "日报",
        "总结", "回顾", "汇报", "述职", "考核", "评估",
        "成果", "成绩", "业绩", "绩效", "目标", "完成",
        "工作汇报", "工作总结", "年度报告", "季度报告",
    ]),
    # Minimalist & Modern Design
    ("扁平简约", [
        "简约", "简洁", "简单", "极简", "现代", "当代",
        "设计", "视觉", "ui", "ux", "用户体验", "用户界面",
        "科技感", "数字感", "数据", "图表", "图形", "信息图",
        "分析", "统计", "报表", "dashboard", "仪表板",
        "互联网", "web", "移动", "app", "应用", "软件",
    ]),
    # Chinese Traditional
    ("中国风", [
        "中国", "中华", "传统", "古典", "古风", "古代",
        "文化", "文明", "历史", "国学", "东方", "水墨",
        "书法", "国画", "诗词", "古文", "经典", "传统节日",
        "春节", "中秋", "端午", "节气", "风水", "易经",
        "儒", "道", "佛", "禅", "茶道", "瓷器", "丝绸",
    ]),
    # Cultural & Artistic
    ("文化艺术", [
        "文化", "艺术", "文艺", "美学", "审美", "创意",
        "创作", "作品", "展览", "博物馆", "美术馆", "画廊",
        "音乐", "舞蹈", "戏剧", "戏曲", "电影", "影视",
        "摄影", "绘画", "雕塑", "建筑", "设计", "时尚",
        "文学", "诗歌", "小说", "散文", "哲学", "思想",
    ]),
    # Artistic & Fresh
    ("文艺清新", [
        "文艺", "清新", "小清新", "治愈", "温暖", "温柔",
        "浪漫", "唯美", "优雅", "精致", "细腻", "柔和",
        "自然", "生态", "环保", "绿色", "植物", "花卉",
        "风景", "旅行", "游记", "生活", "日常", "情感",
    ]),
    # Creative & Fun
    ("创意趣味", [
        "创意", "创新", "创造", "发明", "新奇", "新颖",
        "独特", "个性", "特色", "趣味", "有趣", "好玩",
        "幽默", "搞笑", "笑话", "娱乐", "休闲", "放松",
        "脑洞", "想象力", "灵感", "点子", "想法", "概念",
    ]),
]

DEFAULT_CATEGORY = "默认"


def suggest_category(query: str) -> str:
    """Deterministic category suggestion for a PPT topic (keyword table,
    table order = priority; falls back to 默认)."""
    query_lower = (query or "").lower()
    for category, keywords in CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in query_lower:
                return category
    return DEFAULT_CATEGORY


def pick_theme_for_category(themes: List[Dict[str, Any]],
                            category: str) -> Optional[Dict[str, Any]]:
    """Pick a random theme of ``category`` (weighted toward specific styles
    over 默认, mirroring the source skill's weighted selection); None when
    nothing matches at all."""
    categorized: Dict[str, List[Dict[str, Any]]] = {}
    for theme in themes:
        style_names = theme.get("style_name_list") or []
        matched = next(
            (name for name in style_names
             if name in dict(CATEGORY_KEYWORDS) or name == DEFAULT_CATEGORY),
            None)
        key = matched or DEFAULT_CATEGORY
        categorized.setdefault(key, []).append(theme)

    pool = categorized.get(category) or []
    if pool:
        return random.choice(pool)

    # Category empty → weighted random across whatever exists (specific
    # styles weigh 2.0, 默认 weighs 0.5 — source skill's weighting)
    available = [(cat, items) for cat, items in categorized.items() if items]
    if not available:
        return None
    cats = [cat for cat, _ in available]
    weights = [0.5 if cat == DEFAULT_CATEGORY else 2.0 for cat in cats]
    total = sum(weights)
    weights = [w / total for w in weights]
    chosen = random.choices(cats, weights=weights, k=1)[0]
    return random.choice(categorized[chosen])


# ============================================================================
# PPT generation — port of generate_ppt.py (outline → generate by outline)
# ============================================================================

def generate_ppt_blocking(api_key: str, query: str,
                          style_id: int = 0, tpl_id: Optional[int] = None,
                          web_content: Optional[str] = None,
                          progress_hook=None) -> Dict[str, Any]:
    """Run the two-phase Baidu generation (outline → PPT by outline) to
    completion and return the FINAL event dict (is_end=True carries
    data.ppt_url).

    Blocking by design (2-5 minutes): the stage guard runs it via
    ``asyncio.to_thread``. ``progress_hook(seconds, status)`` observes the
    streaming progress events (observability/logging only).
    """
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "X-Appbuilder-From": "openclaw",
    }

    # Phase 1: outline (SSE stream; first event carries chat_id/query_id)
    outline_headers = dict(headers)
    outline_headers.setdefault("Accept", "text/event-stream")
    outline_headers.setdefault("Cache-Control", "no-cache")
    outline_headers.setdefault("Connection", "keep-alive")
    title, outline, chat_id, query_id = "", "", "", ""
    with requests.post(API_BASE + "generate_outline", headers=outline_headers,
                       json={"query": query},
                       timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                       stream=True) as response:
        for line in response.iter_lines():
            line = line.decode("utf-8")
            if line and line.startswith("data:"):
                delta = json.loads(line[5:].strip())
                if not title:
                    title = delta["title"]
                    chat_id = delta["chat_id"]
                    query_id = delta["query_id"]
                outline += delta["outline"]

    # Phase 2: PPT generation by outline (SSE stream until is_end)
    params = {
        "query_id": int(query_id),
        "chat_id": int(chat_id),
        "query": query,
        "outline": outline,
        "title": title,
        "style_id": style_id,
        "tpl_id": tpl_id,
        "web_content": web_content,
        "enable_save_bos": True,
    }
    start_time = int(time.time())
    with requests.post(API_BASE + "generate_ppt_by_outline", headers=headers,
                       json=params,
                       timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                       stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"generate_ppt_by_outline HTTP {response.status_code}: "
                f"{response.text[:200]}")
        final: Dict[str, Any] = {}
        for line in response.iter_lines():
            line = line.decode("utf-8")
            if not (line and line.startswith("data:")):
                continue
            event = json.loads(line[5:].strip())
            if event.get("is_end"):
                final = event
            elif progress_hook:
                progress_hook(int(time.time()) - start_time,
                              event.get("status", "生成中"))
        if not final:
            raise RuntimeError("生成流结束但未收到 is_end 事件")
        return final


# ============================================================================
# Tool registration (toolset "ppt_gen") — app-scoped visibility; the FSM
# pattern declares NO allow_toolset, so these stay ungrantable-by-default
# (the stage path calls the functions above directly, deterministic)
# ============================================================================

def _handle_list_themes(args: dict) -> str:
    api_key = get_api_key()
    if not api_key:
        return tool_error("BAIDU_API_KEY 环境变量未设置")
    try:
        themes = fetch_ppt_themes(api_key)
    except Exception as e:  # noqa: BLE001 — tool boundary, report honestly
        return tool_error(f"获取模板列表失败: {e}")
    return tool_result(success=True, count=len(themes), themes=themes)


def _handle_generate(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query（PPT主题）不能为空")
    api_key = get_api_key()
    if not api_key:
        return tool_error("BAIDU_API_KEY 环境变量未设置")
    try:
        final = generate_ppt_blocking(
            api_key, query,
            style_id=int(args.get("style_id") or 0),
            tpl_id=args.get("tpl_id"),
            web_content=args.get("web_content"),
        )
    except Exception as e:  # noqa: BLE001 — tool boundary, report honestly
        return tool_error(f"PPT 生成失败: {e}")
    return tool_result(success=True, result=final)


registry.register(
    name="ppt_gen_list_themes",
    toolset="ppt_gen",
    schema={
        "description": "列出百度文库 AI PPT 的可用模板（风格名/style_id/tpl_id）",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=_handle_list_themes,
)

registry.register(
    name="ppt_gen_generate",
    toolset="ppt_gen",
    schema={
        "description": "用百度文库 AI 按 PPT 主题生成一份 PPT（2-5 分钟），"
                       "返回含 ppt_url 的最终结果",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PPT主题/内容"},
                "style_id": {"type": "integer", "description": "风格ID，默认 0"},
                "tpl_id": {"type": "integer", "description": "模板ID（可选）"},
                "web_content": {"type": "string", "description": "参考网页内容（可选）"},
            },
            "required": ["query"],
        },
    },
    handler=_handle_generate,
)
