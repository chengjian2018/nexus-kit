"""Vision (multimodal) message helpers.

Messages are plain dicts end-to-end and providers forward them verbatim,
so an OpenAI-style content-parts array reaches the API untouched. These
helpers build those parts and answer "can this model read images?" from
the provider registry — the only two things a vision-capable reviewer
station needs.

    content = multimodal_user_content("review these screenshots", [p1, p2])
    messages = [{"role": "user", "content": content}]
"""

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Default per-image byte cap: base64 inflates 1.33x, and a tens-of-MB full-
# page screenshot can push the request body past each API's per-request
# limit (400/413) — a failure that lands in post-delivery stages like review
# where silent breakage is very hard to diagnose. Pre-check before reading;
# over the cap raises honestly.
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def image_data_url(
        path: Union[str, Path],
        max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> str:
    """Read *path* and return a ``data:<mime>;base64,...`` URL.

    Raises ValueError when the file exceeds *max_bytes* (pre-checked via
    stat before any read), keeping the caller's honest-degradation contract.
    """
    p = Path(path)
    size = p.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"图片 {p} 体积 {size} 字节超过上限 {max_bytes}"
            f"(请压缩或降分辨率后重试)")
    mime = _MIME_BY_SUFFIX.get(p.suffix.lower(), "application/octet-stream")
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def multimodal_user_content(
    text: str, image_paths: List[Union[str, Path]]
) -> List[Dict[str, Any]]:
    """OpenAI-style user content parts: one text part + image_url parts.

    A missing/unreadable image raises — callers that must degrade
    honestly (never fabricate a review) check paths beforehand.
    """
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for path in image_paths:
        parts.append({
            "type": "image_url",
            "image_url": {"url": image_data_url(path)},
        })
    return parts


def vision_status(provider_code: str, model: str) -> Optional[bool]:
    """Whether *provider_code*/*model* accepts image input.

    ``None`` = unknown (provider not registered, or registered without a
    ``vision_models`` declaration) — callers may attempt the call and let
    the API answer; ``False`` = declared not vision-capable, the caller
    should degrade honestly instead of sending images into a text-only
    model.
    """
    # Local import: the registry imports llm.provider; keep this module
    # importable from provider code without a cycle.
    from nexus.registry.providers import registry

    entry = registry.get(provider_code)
    if entry is None:
        return None
    return entry.supports_vision(model)
