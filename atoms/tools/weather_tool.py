"""Weather query tool — mock weather lookup.

Automatically registered via ``registry.register()`` at module import; no manual setup needed.

Tool name: ``weather_query``
Toolset: ``utility``
Permission: all patterns, all modules (``{"*": True}``).
"""

import json
from typing import Any, Dict

from nexus.registry.tools import registry, tool_error, tool_result

# ---------------------------------------------------------------------------
# Mock weather data
# ---------------------------------------------------------------------------

_MOCK_WEATHER: Dict[str, Dict[str, Any]] = {
    "北京": {
        "temperature": 28,
        "humidity": 65,
        "condition": "晴",
        "wind": "北风 3级",
        "aqi": 52,
    },
    "上海": {
        "temperature": 32,
        "humidity": 78,
        "condition": "多云转阵雨",
        "wind": "东南风 4级",
        "aqi": 45,
    },
    "广州": {
        "temperature": 35,
        "humidity": 85,
        "condition": "雷阵雨",
        "wind": "南风 2级",
        "aqi": 38,
    },
    "深圳": {
        "temperature": 33,
        "humidity": 82,
        "condition": "多云",
        "wind": "西南风 3级",
        "aqi": 30,
    },
    "杭州": {
        "temperature": 30,
        "humidity": 72,
        "condition": "阴转小雨",
        "wind": "东北风 3级",
        "aqi": 55,
    },
    "成都": {
        "temperature": 26,
        "humidity": 80,
        "condition": "小雨",
        "wind": "北风 2级",
        "aqi": 42,
    },
}

_DEFAULT_WEATHER = {
    "temperature": 25,
    "humidity": 60,
    "condition": "晴间多云",
    "wind": "微风",
    "aqi": 50,
}


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _handle_weather(args: Dict[str, Any]) -> str:
    """Handle weather_query tool calls.

    Args:
        args: Dictionary containing the ``city`` key (city name).

    Returns:
        JSON string with weather info or error message.
    """
    city = args.get("city", "")
    if not city or not isinstance(city, str):
        return tool_error("请提供有效的城市名称", city=city)

    city = city.strip()
    weather = _MOCK_WEATHER.get(city, _DEFAULT_WEATHER)

    return tool_result({
        "city": city,
        "temperature": weather["temperature"],
        "humidity": weather["humidity"],
        "condition": weather["condition"],
        "wind": weather["wind"],
        "aqi": weather["aqi"],
        "unit": "摄氏度",
        "note": "（模拟数据）" if city not in _MOCK_WEATHER else "",
    })


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WEATHER_SCHEMA = {
    "name": "weather_query",
    "description": (
        "查询指定城市的天气信息。返回温度、湿度、天气状况、风力、空气质量等。"
        "支持的城市: 北京、上海、广州、深圳、杭州、成都。"
        "其他城市将返回默认模拟数据。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "要查询天气的城市名称，如 '北京'、'上海'。",
            }
        },
        "required": ["city"],
    },
}


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register(
    name="weather_query",
    toolset="utility",
    schema=WEATHER_SCHEMA,
    handler=_handle_weather,
    description="查询指定城市的天气信息（模拟数据）",
    emoji="🌤️",
    allowed_patterns={"*": True},
)