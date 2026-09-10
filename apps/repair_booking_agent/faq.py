"""Repair-booking outbound-call FAQ — keyword table for the custom clarify
stage (业务检测只使用关键词卡控, no vector recall / score gating).

Same matching contract as the install app's table (specific-first keyword
containment, any-keyword hit ⇒ kb track, answers may reference task_info
fields); the repair scenario swaps the question families: the customer on
a repair call asks about fees / warranty scope / what the technician
brings / self-fix options / used-furniture policy / progress follow-up —
arrival/logistics questions no longer apply.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Ordered: specific entries first (checked top-down, first hit wins)
FAQ_ENTRIES: List[Dict[str, object]] = [
    {
        "topic": "费用",
        "keywords": ["收费", "费用", "多少钱", "收费吗", "要钱吗", "免费吗",
                     "额外的钱", "收钱", "上门费"],
        "answer": (
            "本次{product_name}的上门维修在保修期内是免收上门费和维修费的；"
            "保外维修师傅会先检测报价，您确认后再修，不修不收钱。"
        ),
    },
    {
        "topic": "保修",
        "keywords": ["保修", "质保", "三包", "过保", "保修期"],
        "answer": (
            "{product_name}整机按国家三包政策提供保修，保修期内非人为损坏"
            "的故障免收上门费和维修费，您放心。"
        ),
    },
    {
        "topic": "维修时长",
        "keywords": ["多久", "多长时间", "修完", "要几个小时", "几个小时",
                     "麻烦吗"],
        "answer": (
            "常规维修大概 1 个小时左右，具体要看故障情况，师傅上门检测后"
            "会先告诉您大概时间，一般不影响您正常安排。"
        ),
    },
    {
        "topic": "配件",
        "keywords": ["带配件", "带零件", "换零件", "有配件吗", "原厂件",
                     "配件多少钱"],
        "answer": (
            "师傅上门会带常用配件，检测后如需更换会先报价，您确认后再换；"
            "常用配件一般当场就能换好。"
        ),
    },
    {
        "topic": "自修咨询",
        "keywords": ["自己修", "我自己会修", "不用师傅", "自修", "指导一下怎么修"],
        "answer": (
            "电器故障不建议您自行拆修哦，安全第一；建议还是让师傅上门"
            "检测，师傅会当面排查故障原因，修不好您也可以不修。"
        ),
    },
    {
        "topic": "进度查询",
        "keywords": ["什么时候来", "几点到", "迟到", "怎么还没来", "师傅到哪了"],
        "answer": (
            "维修进度这边帮您记录反馈，师傅上门前会提前电话联系您，"
            "您留意下来电就好。"
        ),
    },
]


def match_faq(text: str) -> Optional[Dict[str, object]]:
    """Pure keyword containment against the FAQ table (specific-first).

    Args:
        text: the assembled search text (user query + topic + keywords).

    Returns:
        The first entry whose keyword list intersects the text (dict with
        topic / keywords / answer); None when no entry hits.
    """
    if not text:
        return None
    for entry in FAQ_ENTRIES:
        if any(kw in text for kw in entry["keywords"]):  # type: ignore[arg-type]
            return entry
    return None
