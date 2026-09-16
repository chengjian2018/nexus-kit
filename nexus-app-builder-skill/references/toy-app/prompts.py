"""Prompt assets for note_polish — kept in their own module so route.py
stays a pure declaration and executors stay pure mechanics."""

NOTE_POLISH_BASE_PROMPT = (
    "你是一名严谨的笔记整理助手。你的产出总是 Markdown 备忘录，"
    "结构为：## 主题 / ## 决议 / ## 待办（含负责人与期限） / ## 风险。"
    "忠实于用户的原始笔记：可以重组与提炼，不得编造未提及的事实。"
)

# np_draft: {request} = the user's original message (from history).
DRAFT_PHASE_PROMPT = """\
请把下面的原始笔记整理为备忘初稿，直接输出备忘全文（Markdown，不要额外说明）。

### 原始笔记
{request}\
"""

# np_review: JSON protocol — one object, nothing else. {memo} = current bytes.
REVIEW_PHASE_PROMPT = """\
请评审下面的备忘初稿。只输出一个 JSON 对象（不要 Markdown 代码围栏）：
{{"verdict": "pass" | "fail",
  "issues": ["问题描述1", "问题描述2", ...]}}

判定标准：结构完整（四节齐全）、忠于原始笔记、待办有负责人或明确标注缺位、
无明显冗余。仅在确有必须修复的问题时给 fail；吹毛求疵不是 pass 的反面。
issues 按严重程度降序，最多 5 条。verdict=pass 时 issues 为空数组。

### 当前备忘
{memo}\
"""

# One self-correct retry when the JSON doesn't parse (archify route_retries idiom).
REVIEW_RETRY_PROMPT = """\
上一条输出不是合法 JSON（错误：{error}）。请重新只输出一个 JSON 对象：
{{"verdict": "pass"|"fail", "issues": [...]}}，不要任何其他文字。\
"""

# np_revise: the experience-inheritance prompt. This is the whole point of
# the toy — round N sees everything rounds 1..N-1 learned.
REVISE_PHASE_TMPL = """\
请修订下面的备忘。直接输出修订后的备忘全文（Markdown，不要额外说明）。

修订纪律：
- 优先处理"本轮评审"中标号靠前的问题；
- 对照"修订历史"——已尝试且未生效的改法不要原样重演，换一种思路；
- 未被评审点名的内容保持原样（最小改动原则）。

### 原始请求
{request}

### 当前备忘
{memo}

### 历轮评审（全部）
{critique_history}

### 修订历史（全部）
{revision_history}\
"""
