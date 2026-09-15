"""Vision (multimodal) message helpers.

Messages are plain dicts end-to-end and providers forward them verbatim,
so an OpenAI-style content-parts array reaches the API untouched. These
helpers build those parts and answer "can this model read images?" from
the provider registry — the only two things a vision-capable reviewer
station needs.

    content = multimodal_user_content("评审这几张截图", [p1, p2])
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


def image_data_url(path: Union[str, Path]) -> str:
    """Read *path* and return a ``data:<mime>;base64,...`` URL."""
    p = Path(path)
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
