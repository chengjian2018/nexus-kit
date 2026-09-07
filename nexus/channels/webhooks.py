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

import logging
import os
import time
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from nexus.channels.base import EngineOps

logger = logging.getLogger(__name__)


def build_channel_router(spec: Any, ops: EngineOps) -> APIRouter:
    """Build a router for a single ChannelSpec: POST /api/v1/channel/{spec.name}."""
    router = APIRouter()
    payload_model = spec.payload_model

    @router.post(f"/api/v1/channel/{spec.name}")
    def handle(
        payload: payload_model,  # type: ignore[valid-type]
        token: str = Query(default=""),
    ) -> Dict[str, Any]:
        # 1. Optional shared secret: validation is enabled only when the env var is set to a non-empty value
        if spec.token_env:
            expected = os.getenv(spec.token_env)
            if expected and token != expected:
                raise HTTPException(status_code=403, detail="channel token 校验失败")

        # 2. Channel difference point (1): payload -> normalized message
        msg = spec.parse(payload)

        # 3. Staleness filtering (reconnect replay protection): a normal
        # business path; swallowed with an empty reply
        session_id = f"{spec.name}:{msg.session_key}"
        if msg.timestamp is not None and time.time() - msg.timestamp > spec.stale_seconds:
            logger.info(
                "[%s] 丢弃过期消息: session=%s stale_seconds=%.0f",
                spec.name, session_id, spec.stale_seconds,
            )
            return spec.build_reply("", session_id)

        # 4. get-or-create: auto-launch with the default pattern when no session exists
        session = ops.get_session(session_id)
        if session is None:
            pattern_code = os.getenv(spec.default_pattern_env)
            if not pattern_code:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"会话 '{session_id}' 不存在且未配置默认 pattern"
                        f"（设置环境变量 {spec.default_pattern_env} 后重试）"
                    ),
                )
            request_id = f"{spec.name}-{uuid.uuid4().hex[:12]}"
            session, _code, message = ops.launch_session(
                pattern_code, session_id, msg.task_info, request_id, exist_ok=True,
            )
            if session is None:
                raise HTTPException(status_code=500, detail=f"自动 launch 失败: {message}")
            logger.info("[%s] 自动 launch: session=%s pattern=%s",
                        spec.name, session_id, pattern_code)

        # 5. One chat turn + channel difference point (2): success response contract
        reply, error = ops.run_chat_turn(session, msg.text)
        if error is not None:
            raise HTTPException(status_code=500, detail=f"对话处理异常: {error}")

        logger.info("[%s] 回复: session=%s reply_len=%d",
                    spec.name, session_id, len(reply or ""))
        return spec.build_reply(reply or "", session_id)

    return router


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
