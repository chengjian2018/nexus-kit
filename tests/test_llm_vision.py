"""nexus/llm/vision.py 单元测试 — data URL 编码、多模态 parts 组装、
vision_status 三态(声明/未声明/未注册)。"""

import base64

from nexus.llm.vision import (
    image_data_url,
    multimodal_user_content,
    vision_status,
)
from nexus.registry.providers import registry

# zai 由 test_zai_provider / 宿主装载链注册;这里显式兜底导入一次
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

    # 未知后缀:八进制流 mime(不猜图片格式)
    raw = tmp_path / "blob.bin"
    raw.write_bytes(b"x")
    assert image_data_url(raw).startswith("data:application/octet-stream")


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
    # 已声明:flash 是视觉模型,glm-5.3 不是
    assert vision_status("zai", "glm-5.3-flash") is True
    assert vision_status("zai", "glm-5.3") is False
    # 未注册的 provider(如测试注入的 code "x"):未知 → None,调用方可尝试
    assert registry.get("no-such-provider") is None
    assert vision_status("no-such-provider", "any") is None
    # dashscope 未声明 vision_models → None(未知)
    import atoms.providers.dashscope_provider  # noqa: F401

    assert vision_status("dashscope", "qwen3.8-max") is None
