"""天气解析回归测试。

历史 Bug: 高德天气接口返回的逐日数组字段名是 `casts`(而不是 `forecast`),
之前代码读的是 `forecast`, 导致永远解析出空列表, 所有日期都显示"暂无预报"。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.trip_planner_agent import MultiAgentTripPlanner  # noqa: E402
from app.services.amap_service import AmapClient  # noqa: E402

# 高德真实返回结构(节选)
AMAP_WEATHER_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "forecasts": [
        {
            "city": "北京市",
            "adcode": "110000",
            "reporttime": "2026-11-27 09:39:25",
            "casts": [
                {
                    "date": "2026-11-27",
                    "week": "5",
                    "dayweather": "晴",
                    "nightweather": "多云",
                    "daytemp": "10",
                    "nighttemp": "0",
                    "daywind": "西北",
                    "daypower": "1-3",
                },
                {
                    "date": "2026-11-28",
                    "week": "6",
                    "dayweather": "多云",
                    "nightweather": "晴",
                    "daytemp": "11",
                    "nighttemp": "-1",
                    "daywind": "西北",
                    "daypower": "1-3",
                },
            ],
        }
    ],
}


@pytest.mark.asyncio
async def test_weather_reads_casts_field(monkeypatch):
    """高德返回的是 casts 字段, 必须能解析出来。"""
    client = AmapClient(api_key="dummy")

    async def fake_get(path, params):
        assert path == "/weather/weatherInfo"
        return AMAP_WEATHER_PAYLOAD

    monkeypatch.setattr(client, "_get", fake_get)

    result = await client.weather("北京")
    assert len(result) == 2
    assert result[0]["date"] == "2026-11-27"
    assert result[1]["day_weather"] == "多云"


@pytest.mark.asyncio
async def test_weather_keeps_negative_temperature(monkeypatch):
    """负温度必须保留负号(-1 而不是 1)。"""
    client = AmapClient(api_key="dummy")

    async def fake_get(path, params):
        return AMAP_WEATHER_PAYLOAD

    monkeypatch.setattr(client, "_get", fake_get)

    result = await client.weather("北京")
    assert result[1]["night_temp"] == -1
    assert result[1]["day_temp"] == 11


@pytest.mark.asyncio
async def test_weather_empty_payload_returns_empty_list(monkeypatch):
    """接口返回空数据时不应抛异常, 返回空列表即可。"""
    client = AmapClient(api_key="dummy")

    async def fake_get(path, params):
        return {"status": "1", "forecasts": []}

    monkeypatch.setattr(client, "_get", fake_get)

    assert await client.weather("北京") == []


def test_build_weather_info_maps_to_trip_dates():
    """预报日期能对上行程日期, 对不上的标记为暂无预报。"""
    forecast = [
        {
            "date": "2026-11-28",
            "day_weather": "多云",
            "night_weather": "晴",
            "day_temp": 11,
            "night_temp": -1,
            "wind_direction": "西北",
            "wind_power": "1-3",
        }
    ]

    info = MultiAgentTripPlanner._build_weather_info(["2026-11-28", "2026-12-01"], forecast)

    assert info[0].day_weather == "多云"
    assert info[0].day_temp == 11
    assert info[0].night_temp == -1
    # 超出高德 4 天预报范围的日期
    assert info[1].day_weather == "暂无预报"
