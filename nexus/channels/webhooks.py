"""Generic webhook handler — the request-processing line shared by all channels.

Flow (a channel cannot skip any step):
    token validation -> payload validation (pydantic 422) -> parse -> staleness
    filtering -> session prefixing -> get-or-create (pattern env) -> one chat
    turn -> build_reply

Error-code contract (fixed across channels): 403 token / 422 payload /
200 stale message swallowed / 503 no default pattern / 500 launch or chat
error. Staleness is a normal business path and follows the success contract
(empty reply = channel side "do not send"), not an error code.
"""

import hmac
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from nexus.channels.base import EngineOps

logger = logging.getLogger(__name__)


def build_channel_router(spec: Any, ops: EngineOps) -> APIRouter:
    """Build a router for a single ChannelSpec: POST /api/v1/channel/{spec.name}.

    The handler resolves the spec from the registry on **every request**
    (falling back to the assembly-time spec when the name has been
    deregistered): a hot-reloaded replacement spec takes effect on the next
    request — no router rebuild needed. Only the URL path and the payload
    model are frozen at assembly time (FastAPI validates the body before the
    handler runs).
    """
    router = APIRouter()
    payload_model = spec.payload_model

    @router.post(f"/api/v1/channel/{spec.name}")
    async def handle(
        payload: payload_model,  # type: ignore[valid-type]
        token: str = Query(default=""),
    ) -> Dict[str, Any]:
        live = _live_spec(spec)
        # 1. Optional shared secret: validation is enabled only when the env var is set to a non-empty value
        if live.token_env:
            expected = os.getenv(live.token_env)
            if not expected:
                # no token = unauthenticated public endpoint: warn once per
                # request (deployment side should configure it ASAP)
                logger.warning(
                    "[%s] 环境变量 %s 未设置，渠道处于无认证状态（请尽快配置）",
                    live.name, live.token_env,
                )
            elif not hmac.compare_digest(token, expected):
                # constant-time compare, defeating timing side-channel
                # byte-by-byte guessing
                raise HTTPException(status_code=403, detail="channel token 校验失败")

        # 2. Channel difference point (1): payload -> normalized message
        msg = live.parse(payload)

        # 3. Staleness filtering (reconnect replay protection): a normal
        # business path; swallowed with an empty reply. Non-finite timestamps
        # (NaN/Inf parse artifacts) are treated as unfilterable-skipped, and the
        # window is bidirectional — a far-future timestamp cannot freeze a
        # session out of expiry either.
        session_id = f"{live.name}:{msg.session_key}"
        if msg.timestamp is not None and math.isfinite(msg.timestamp) and \
                abs(time.time() - msg.timestamp) > live.stale_seconds:
            logger.info(
                "[%s] 丢弃过期消息: session=%s stale_seconds=%.0f",
                live.name, session_id, live.stale_seconds,
            )
            return live.build_reply("", session_id)

        # 4. get-or-create: auto-launch with the default pattern when no session exists
        session = ops.get_session(session_id)
        if session is None:
            pattern_code = os.getenv(live.default_pattern_env)
            if not pattern_code:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"会话 '{session_id}' 不存在且未配置默认 pattern"
                        f"（设置环境变量 {live.default_pattern_env} 后重试）"
                    ),
                )
            request_id = f"{live.name}-{uuid.uuid4().hex[:12]}"
            session, _code, message = await ops.launch_session(
                pattern_code, session_id, msg.task_info, request_id, exist_ok=True,
            )
            if session is None:
                raise HTTPException(status_code=500, detail=f"自动 launch 失败: {message}")
            logger.info("[%s] 自动 launch: session=%s pattern=%s",
                        live.name, session_id, pattern_code)

        # 5. One chat turn + channel difference point (2): success response contract
        reply, error = await ops.run_chat_turn(session, msg.text)
        if error is not None:
            raise HTTPException(status_code=500, detail=f"对话处理异常: {error}")

        logger.info("[%s] 回复: session=%s reply_len=%d",
                    live.name, session_id, len(reply or ""))
        return live.build_reply(reply or "", session_id)

    return router


def _live_spec(spec: Any) -> Any:
    """Per-request spec resolution: registry value when still registered,
    else the assembly-time spec (a deregistered channel keeps serving its
    frozen form rather than 500ing)."""
    from nexus.registry.channels import registry

    current = registry.get(spec.name)
    return current if current is not None else spec



def build_channel_routers(ops: EngineOps) -> List[APIRouter]:
    """Build a router for every channel in the registry; a failing channel is skipped with a warning."""
    from nexus.registry.channels import registry

    routers: List[APIRouter] = []
    for name in registry.list_names():
        spec = registry.get(name)
        try:
            routers.append(build_channel_router(spec, ops))
        except Exception:
            logger.exception("生成渠道 '%s' router 失败，跳过", name)
    return routers
