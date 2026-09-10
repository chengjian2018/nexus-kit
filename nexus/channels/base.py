"""Channel core protocol — normalized inbound messages, the engine operations bundle, channel declarations.

Channels (webhook-callback style) only implement ChannelSpec to describe their
differences; the common flow (token validation, staleness filtering,
get-or-create, session prefix, error codes) lives entirely in the webhooks.py
generic handler and is structurally impossible to bypass. This module is pure
protocol with no IO; it imports neither the engine nor main.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Tuple, Type, runtime_checkable

from pydantic import BaseModel

from nexus.engine.session import Session


@dataclass
class InboundMessage:
    """The inbound message normalized across all channels.

    timestamp of None means no staleness filtering (the channel parses it on a
    best-effort basis; unparseable means no filtering).
    session_key excludes the channel prefix — the prefix is added uniformly by
    the generic handler.
    """

    channel: str
    text: str
    session_key: str
    timestamp: Optional[float] = None
    task_info: Dict[str, str] = field(default_factory=dict)


@dataclass
class EngineOps:
    """Engine operations bundle injected by main.py — the three core functions shared by endpoints and channels.

    Channel modules depend only on this bundle, never importing main
    (offline unit-testable). Since the asyncio rewrite: get_session stays
    sync (pure in-memory governor lookup); launch_session / run_chat_turn
    are coroutines (store writes + the engine core are async).
    """

    get_session: Callable[[str], Optional[Session]]
    launch_session: Callable[..., Awaitable[Tuple[Optional[Session], str, str]]]
    run_chat_turn: Callable[[Session, str], Awaitable[Tuple[Optional[str], Optional[Exception]]]]


@runtime_checkable
class ChannelSpec(Protocol):
    """The complete declaration of one webhook channel — describes only the differences, no behavior."""

    name: str
    payload_model: Type[BaseModel]
    default_pattern_env: str
    token_env: Optional[str]
    stale_seconds: float

    def parse(self, payload: Any) -> InboundMessage: ...

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]: ...
