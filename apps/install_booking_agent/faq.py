"""Install-booking outbound-call FAQ — keyword table for the custom clarify
stage (业务检测只使用关键词卡控, no vector recall / score gating).

The customer on an outbound install-booking call asks off-flow questions
mid-negotiation ("安装要钱吗", "保修多久", "我自己装行不行"...). The clarify
stage answers from this table and pulls the call back to the booking main
line. Detection is pure keyword containment on the assembled search text
(user query + topic + keywords from the unify stage's clarify signal);
matching strategy:

- specific-first ordering within an entry: a text hitting several entries
  resolves to the earliest (most specific) one — same philosophy as the
  terminal-intent rules;
- any-keyword hit ⇒ kb track (the entry's answer template); no hit ⇒
  fallback track (honest acknowledge + strong pull-back);
- answers may reference task_info fields ({product_name} etc.) — filled by
  the clarify stage, mirroring how answer_examples ground the main line.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Ordered: specific entries first (checked top-down, first hit wins)
FAQ_ENTRIES: List[Dict[str, object]] = [
    {
        "topic": "费用",
        "keywords": ["收费", "费用", "多少钱", "收费吗", "要钱吗", "免费吗",
                     "额外的钱", "收钱"],
        "answer": (
            "本次{product_name}的上门安装是包安装服务，安装本身不额外收费；"
            "如有加打孔、加支架等特殊需求，师傅会先报价，您确认后再做。"
        ),
    },
    {
        "topic": "保修",
        "keywords": ["保修", "质保", "三包", "坏了怎么办", "维修"],
        "answer": (
            "{product_name}整机按国家三包政策提供保修，安装后如果出现质量"
            "问题可以联系商家安排售后，您放心。"
        ),
    },
    {
        "topic": "安装时长",
        "keywords": ["多久", "多长时间", "装完", "要几个小时", "几个小时",
                     "麻烦吗"],
        "answer": (
            "常规安装大概 1 个小时左右，具体看现场情况，师傅上门前会先和"
            "您沟通，一般不影响您正常安排。"
        ),
    },
    {
        "topic": "自装咨询",
        "keywords": ["自己装", "我自己会装", "不用师傅", "自装"],
        "answer": (
            "可以的，您要是方便自己安装也可以不来师傅；不过建议还是让"
            "师傅上门，安装同时会帮您验机调试，后续使用更放心～"
        ),
    },
    {
        "topic": "改地址",
        "keywords": ["换地址", "改地址", "不在地址", "送到别的地方",
                     "另一个地址"],
        "answer": (
            "如果安装地址有变化，建议您先在订单里修改收货地址或联系商家"
            "更新，我们按更新后的地址安排师傅上门。"
        ),
    },
    {
        "topic": "催物流",
        "keywords": ["怎么还没到", "物流怎么这么慢", "快递到哪了", "催一下",
                     "什么时候发货"],
        "answer": (
            "物流进度这边帮您记录反馈，您也可以在订单页查看实时物流；"
            "货到之后我们再约师傅上门就来得及。"
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
