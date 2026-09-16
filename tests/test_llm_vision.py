"""nexus/llm/vision.py unit tests — data-URL encoding, multimodal parts assembly,
the size cap, and the vision_status three states (declared / undeclared /
unregistered)."""

import base64

import pytest

from nexus.llm.vision import (
    DEFAULT_MAX_IMAGE_BYTES,
    image_data_url,
    multimodal_user_content,
    vision_status,
)
from nexus.registry.providers import registry

# zai is registered by test_zai_provider / the host loading chain; an explicit fallback import here
import atoms.providers.zai_provider  # noqa: F401


def test_image_data_url_encodes_bytes_and_mime(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG fake bytes")
    url = image_data_url(png)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG fake bytes"

    jpg = tmp_path / "cam.jpg"
    jpg.write_bytes(b"\xff\xd8 fake")
    assert image_data_url(jpg).startswith("data:image/jpeg;base64,")

    # Unknown suffix: an octet-stream mime (no image-format guessing)
    raw = tmp_path / "blob.bin"
    raw.write_bytes(b"x")
    assert image_data_url(raw).startswith("data:application/octet-stream")


def test_image_data_url_rejects_oversize_before_read(tmp_path):
    """Size guard: stat-checks before reading; over the cap it honestly raises ValueError (the caller handles it per
    the degradation contract), never giving a single giant image the chance
    to blow up the request body."""
    big = tmp_path / "huge.png"
    big.write_bytes(b"x" * 64)
    with pytest.raises(ValueError, match="超过上限"):
        image_data_url(big, max_bytes=32)
    # Exactly at the cap: allowed through
    assert image_data_url(big, max_bytes=64).startswith("data:image/png")
    # The default cap is a constant reference, not swallowable by local mutation
    assert DEFAULT_MAX_IMAGE_BYTES >= 1024


def test_multimodal_user_content_shape(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.webp"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    parts = multimodal_user_content("看这两张", [a, b])
    assert parts[0] == {"type": "text", "text": "看这两张"}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["image_url"]["url"].startswith("data:image/webp;base64,")


def test_vision_status_three_states():
    # Declared: flash is a vision model, glm-5.3 is not
    assert vision_status("zai", "glm-5.3-flash") is True
    assert vision_status("zai", "glm-5.3") is False
    # An unregistered provider (e.g. the test-injected code "x"): unknown → None, the caller may still try
    assert registry.get("no-such-provider") is None
    assert vision_status("no-such-provider", "any") is None
    # dashscope declares no vision_models → None (unknown)
    import atoms.providers.dashscope_provider  # noqa: F401

    assert vision_status("dashscope", "qwen3.8-max") is None
