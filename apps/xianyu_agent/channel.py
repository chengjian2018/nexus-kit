"""Xianyu channel — the bolt-on decision endpoint for xianyu-auto-reply's "default reply API".

Declarative channel (ChannelSpec): payload schema, session derivation,
task_info mapping, success response contract; the common flow (token /
staleness / get-or-create / error codes) lives in webhooks.py.

Response contract (dictated by their parse_api_reply; verify before changing):
- non-200 status -> their side returns None, **nothing is ever sent**; all
  error paths must use non-200
- 200 + non-empty ``reply`` string -> that text is sent; empty ``reply`` ->
  nothing sent ("no reply needed")
- the 200 response body must not carry the string keys data/content/message:
  when reply is empty their side reads those keys in order, and accidental
  keys would leak debug info to the buyer
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator

from nexus.channels.base import InboundMessage
from nexus.registry.channels import registry

# Best-effort parse formats for msg_time (their side promises no format: it may
# be a millisecond timestamp or a common date string)
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


class XianyuInboundMessage(BaseModel):
    """Inbound message payload of the xianyu-auto-reply default reply API."""

    account_id: str  # Xianyu seller account identifier
    message: str  # raw buyer message text
    chat_id: str  # Xianyu conversation ID (buyer x product dimension)
    item_id: Optional[str] = None
    send_user_id: Optional[str] = None
    send_user_name: Optional[str] = None
    msg_time: Optional[str] = None

    @field_validator("account_id", "chat_id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        """ID fields act as session-key/db-key building blocks: limit length + character
        set (alphanumerics and common separator characters). IDs containing ``:``
        etc. are rejected with a 422 to prevent session_key prefix-collision forgery."""
        v = (v or "").strip()
        if not v or len(v) > 64 or not v.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError("ID 字段须为 1-64 位字母数字（可含 -_.）")
        return v


def _parse_msg_time(msg_time: str) -> Optional[float]:
    """Best-effort parse of msg_time into epoch seconds; return None when
    unrecognizable (no staleness filtering applied)."""
    text = msg_time.strip()
    if not text:
        return None
    try:
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            return None  # nan/inf cannot participate in staleness checks: treat as unparseable
        return value / 1000.0 if value > 1e12 else value
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class XianyuChannel:
    """Xianyu channel declaration: session_key is the bare ``account_id:chat_id``
    (the prefix is added by the handler; session_id stays byte-identical to the
    historical ``xianyu:{account_id}:{chat_id}``)."""

    name = "xianyu"
    payload_model = XianyuInboundMessage
    default_pattern_env = "XIANYU_CHANNEL_PATTERN"
    token_env = "XIANYU_CHANNEL_TOKEN"
    stale_seconds = 300.0

    def parse(self, payload: XianyuInboundMessage) -> InboundMessage:
        """Payload -> normalized inbound message (task_info mapping + best-effort msg_time parsing)."""
        task_info: Dict[str, str] = {"channel": "xianyu", "account_id": payload.account_id}
        if payload.item_id is not None:
            task_info["item_id"] = payload.item_id[:64]
        if payload.send_user_id is not None:
            task_info["buyer_user_id"] = payload.send_user_id[:64]
        if payload.send_user_name is not None:
            # Buyer-controlled nickname: truncate to display-only length (it
            # enters task_info which the prompt assembles — keep it small)
            task_info["buyer_user_name"] = payload.send_user_name[:64]
        timestamp = _parse_msg_time(payload.msg_time) if payload.msg_time else None
        return InboundMessage(
            channel=self.name,
            text=payload.message[:4000],
            session_key=f"{payload.account_id}:{payload.chat_id}",
            timestamp=timestamp,
            task_info=task_info,
        )

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]:
        """The success response carries only the reply/session_id keys (contract in the module docstring)."""
        return {"reply": reply, "session_id": session_id}


registry.register(XianyuChannel())
