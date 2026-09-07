"""Session history compression — when the token estimate exceeds the threshold, old messages are LLM-summarized into a summary line + the most recent N messages are retained.

Borrows the compression design of Customer-Agent SessionManager with two deliberate deviations:
- the retain boundary snaps back to the start of the assistant(tool_calls) paired run — a hard
  cut by count can leave a tool row inside the boundary with its assistant row outside, and the
  replay guard then degrades the whole segment (a latent defect in Customer-Agent)
- any exception from the summary LLM call → abandon compression and return history unchanged
  (iron rule: a failed summary never deletes history)

It orchestrates three parties (LLM + store + cxt), hence it lives apart from store (the pure
persistence layer does not couple to the LLM).
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from nexus.context import (
    SessionMessage,
    decode_tool_call_content,
)
from nexus.llm.resolve import build_provider

# Framework-level summary prompt (split out of the old root prompt.py)
HISTORY_SUMMARY_PROMPT = """你是对话摘要助手。请简洁地总结以下对话的要点，保留：
- 用户的核心诉求与已确认的关键信息（如商品、价格、偏好）
- 双方已达成的共识与待办事项
- 已调用过的工具及其关键结论（一笔带过即可）

直接输出摘要正文，不要任何前后缀说明。

### 对话内容（按时间序，共 {__msg_count__} 条）
{__dialog_text__}
"""

if TYPE_CHECKING:
    from nexus.engine.session import Session
    from nexus.engine.store import SessionStore

logger = logging.getLogger(__name__)

# Truncation length per message entering summary concatenation (prevents oversized tool results from blowing up the summary request)
_SUMMARY_MSG_TRUNCATE = 200


# ============================================================================
# Token estimation (character approximation; no tiktoken dependency)
# ============================================================================

def _estimate_text(text: str) -> int:
    """Character-approximation estimate: CJK×2 + others×0.25 (the Customer-Agent fallback formula)."""
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return int(cjk * 2 + (len(text) - cjk) * 0.25)


def estimate_tokens(history: List[SessionMessage]) -> int:
    """Total token estimate of a message list: each message's content (including tool-turn JSON payloads) + 4 per message."""
    total = 0
    for msg in history:
        total += 4
        total += _estimate_text(msg.content)
    return total


def should_compress(
    history: List[SessionMessage], threshold: int, retain_count: int
) -> bool:
    """Threshold > 0, estimate over the threshold, and enough messages to be worth compressing (old messages exist to summarize)."""
    if threshold <= 0 or len(history) <= retain_count + 1:
        return False
    return estimate_tokens(history) > threshold


# ============================================================================
# Compression execution
# ============================================================================

def _snap_to_pair_boundary(history: List[SessionMessage], split: int) -> int:
    """Snap split back onto the start of the assistant(tool_calls) paired run.

    If split lands on a tool row (whose run starts at the earlier assistant tool turn),
    move split back to before that assistant row — no paired fragment is left outside
    the boundary.
    """
    while 0 < split < len(history) and history[split].role == "tool":
        start = split - 1
        while (start >= 0 and history[start].role == "tool"):
            start -= 1
        if (start >= 0 and history[start].role == "assistant"
                and decode_tool_call_content(history[start].content) is not None):
            split = start
        else:
            break
    return split


def _build_summary_input(history: List[SessionMessage], end_idx: int) -> str:
    """Concatenate the old messages into the summary request text (role annotation + per-message truncation).

    An assistant tool-turn content is a JSON payload: take the inner text + annotate the called tool names.
    """
    lines = []
    for msg in history[:end_idx]:
        content = (msg.content or "")[:_SUMMARY_MSG_TRUNCATE]
        decoded = decode_tool_call_content(msg.content) if msg.role == "assistant" else None
        if decoded is not None:
            text, tool_calls = decoded
            names = ",".join(
                tc.get("function", {}).get("name", "?")
                for tc in tool_calls
            )
            content = f"{text} [调用工具: {names}]".strip()
        lines.append(f"[{msg.role}]: {content}")
    return "\n".join(lines)


def compress_history(
    session: "Session",
    store: "SessionStore",
    llm_config: Dict[str, Any],
    retain_count: int,
) -> bool:
    """Execute compression: LLM-summarize the old messages → summary line + retain the most recent messages.

    Returns:
        Whether compression succeeded. Any failing step (DB mismatch / LLM error) returns False
        with the DB and cxt.history left untouched.
    """
    cxt = session.cxt
    split = _snap_to_pair_boundary(
        cxt.history, max(0, len(cxt.history) - retain_count))
    if split <= 0:
        return False  # everything is inside the retention window; no old messages to summarize

    # DB/memory alignment check: under write-through the two should agree; if they do not
    # (e.g. the sink once failed) never delete — replace_history re-validates inside the
    # transaction (transaction-level fallback)
    try:
        db_history = store.get_history(session.session_id)
    except Exception:
        logger.exception("压缩前读 DB 失败，放弃: session=%s", session.session_id)
        return False
    if len(db_history) != len(cxt.history):
        logger.warning(
            "DB/内存消息数不齐，放弃压缩: session=%s db=%d mem=%d",
            session.session_id, len(db_history), len(cxt.history),
        )
        return False

    # Summary LLM call (reuses the llm_config of the current position — just refreshed by R1)
    prompt = HISTORY_SUMMARY_PROMPT.replace(
        "{__msg_count__}", str(split)
    ).replace(
        "{__dialog_text__}", _build_summary_input(cxt.history, split)
    )
    try:
        provider = build_provider(llm_config)
        result = provider.chat_completion(
            messages=[
                {"role": "system", "content": "你是一个对话摘要助手。"},
                {"role": "user", "content": prompt},
            ],
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.3),
            max_tokens=llm_config.get("max_tokens", 1024),
        )
        summary = (result.get("content", "") or "").strip()
    except Exception:
        logger.exception(
            "摘要 LLM 调用失败，放弃压缩（历史原样保留）: session=%s",
            session.session_id,
        )
        return False
    if not summary:
        logger.warning("摘要为空，放弃压缩: session=%s", session.session_id)
        return False

    # DB reshuffle (one transaction: alignment check → delete all rows of the current generation → summary first + reinsert retained)
    try:
        store.replace_history(session, summary, keep_idx=split)
    except Exception:
        logger.exception(
            "DB 压缩重排失败，放弃（历史原样保留）: session=%s",
            session.session_id,
        )
        return False

    # Rebuild history in memory + fix the in-turn marker (not resetting it would inject the query twice)
    summary_msg = SessionMessage(
        role="summary", content=summary, stage="compress")
    cxt.history = [summary_msg] + list(cxt.history[split:])
    cxt.turn_history_start = len(cxt.history)
    logger.info(
        "历史压缩完成: session=%s 摘要=%d 条 保留=%d 条 (split=%d)",
        session.session_id, split, len(cxt.history) - 1, split,
    )
    return True


def maybe_compress(session: "Session", store: "SessionStore") -> None:
    """Compression trigger entry (called by chat_turn after R1 and before adding the user message).

    store None / threshold 0 / too few messages → skip silently; failures are only logged —
    compression is an optimization and must never block the dialogue.
    """
    if store is None:
        return
    try:
        from nexus.settings import get_session_compress_config
        threshold, retain_count = get_session_compress_config()
    except Exception:
        logger.exception("读取压缩配置失败，跳过压缩")
        return
    if not should_compress(session.cxt.history, threshold, retain_count):
        return
    llm_config = session.cxt.llm_config
    if not llm_config:
        logger.warning("llm_config 未解析，跳过压缩: session=%s",
                       session.session_id)
        return
    logger.info(
        "触发历史压缩: session=%s 估算 tokens=%d 阈值=%d",
        session.session_id,
        estimate_tokens(session.cxt.history),
        threshold,
    )
    try:
        compress_history(session, store, llm_config, retain_count)
    except Exception:
        logger.exception(
            "压缩执行异常（历史原样保留）: session=%s", session.session_id)
