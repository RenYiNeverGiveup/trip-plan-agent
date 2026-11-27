"""测试用的假 LLM / 假高德客户端。

不依赖网络与真实 API Key, 用于验证整条流水线的编排逻辑:
工具调用 -> 候选采集 -> 规划选择 -> 路线优化 -> 预算汇总。
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.types import ChatMessage, ToolCall  # noqa: E402


# ---------------------------------------------------------------------- #
# 假数据
# ---------------------------------------------------------------------- #
def _poi(poi_id: str, name: str, lng: float, lat: float, rating: float, ticket: int = 0, kind: str = "风景名胜") -> Dict[str, Any]:
    return {
        "id": poi_id,
        "name": name,
        "address": f"{name}地址",
        "location": {"longitude": lng, "latitude": lat},
        "type": kind,
        "typecode": "01000000",
        "tel": "010-00000000",
        "rating": rating,
        "cost": None,
        "tag": kind,
        "ticket_price": ticket,
        "photos": [f"https://example.com/{poi_id}.jpg"],
    }


ATTRACTION_POIS = [
    _poi("A001", "故宫博物院", 116.397, 39.918, 4.8, 60),
    _poi("A002", "天坛公园", 116.407, 39.883, 4.6, 34),
    _poi("A003", "颐和园", 116.276, 39.999, 4.7, 30),
    _poi("A004", "什刹海", 116.385, 39.940, 4.4, 0),
]

HOTEL_POIS = [
    _poi("H001", "王府井便捷酒店", 116.410, 39.915, 4.3, kind="酒店"),
    _poi("H002", "前门商务酒店", 116.397, 39.899, 4.5, kind="酒店"),
]

RESTAURANT_POIS = [
    _poi("R001", "老北京炸酱面", 116.396, 39.917, 4.5, kind="餐饮服务"),
    _poi("R002", "川味人家", 116.399, 39.919, 4.3, kind="餐饮服务"),
    _poi("R003", "清真小馆", 116.395, 39.915, 4.2, kind="餐饮服务"),
]


class FakeAmapClient:
    """高德客户端替身。"""

    def __init__(self) -> None:
        self.route_calls = 0

    async def search_poi(self, keywords: str, city: str, limit: int = 10, citylimit: bool = True):
        if "酒店" in keywords or "宾馆" in keywords:
            return [dict(item) for item in HOTEL_POIS]
        return [dict(item) for item in ATTRACTION_POIS]

    async def search_around(self, longitude: float, latitude: float, keywords: str = "", radius: int = 3000, limit: int = 15):
        return [dict(item) for item in RESTAURANT_POIS]

    async def poi_detail(self, poi_id: str):
        for item in ATTRACTION_POIS + HOTEL_POIS + RESTAURANT_POIS:
            if item["id"] == poi_id:
                return dict(item)
        raise RuntimeError(f"未找到 POI: {poi_id}")

    async def weather(self, city: str):
        return [
            {
                "date": "2025-06-01",
                "day_weather": "晴",
                "night_weather": "多云",
                "day_temp": 28,
                "night_temp": 19,
                "wind_direction": "南风",
                "wind_power": "1-3级",
            },
            {
                "date": "2025-06-02",
                "day_weather": "多云",
                "night_weather": "晴",
                "day_temp": 27,
                "night_temp": 18,
                "wind_direction": "东风",
                "wind_power": "3-4级",
            },
        ]

    async def route(self, origin, destination, mode: str = "walking", city: Optional[str] = None,
                    origin_poi_id: Optional[str] = None, destination_poi_id: Optional[str] = None):
        self.route_calls += 1
        import math

        lon1, lat1 = origin
        lon2, lat2 = destination
        km = math.hypot((lon1 - lon2) * 85, (lat1 - lat2) * 111)
        return {"distance_m": int(km * 1000), "duration_s": int(km * 1000 / 4.5 * 60), "mode": mode}

    async def geocode(self, address: str, city: Optional[str] = None):
        return {"longitude": 116.397, "latitude": 39.916}


# ---------------------------------------------------------------------- #
# 假 LLM
# ---------------------------------------------------------------------- #
class FakeLLM:
    """按提示词身份返回预置结果的 LLM 替身。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    # -- 工具调用 ------------------------------------------------------- #
    @staticmethod
    def _system_prompt(messages: List[ChatMessage]) -> str:
        return messages[0].content or "" if messages and messages[0].role == "system" else ""

    @staticmethod
    def _last_tool_message(messages: List[ChatMessage]) -> Optional[ChatMessage]:
        for message in reversed(messages):
            if message.role == "tool":
                return message
        return None

    @staticmethod
    def _context(messages: List[ChatMessage]) -> Dict[str, Any]:
        for message in reversed(messages):
            if message.role != "user" or "【上下文数据(JSON)】" not in (message.content or ""):
                continue
            payload = (message.content or "").split("【上下文数据(JSON)】", 1)[1].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {}
        return {}

    async def chat(self, messages, tools=None, tool_choice=None, response_format=None, temperature=None):
        self.calls.append({"tools": bool(tools), "n_messages": len(messages)})
        prompt = self._system_prompt(messages)
        tool_message = self._last_tool_message(messages)

        # 已经调用过工具 -> 输出最终 JSON
        context = self._context(messages)

        if tool_message is not None:
            return ChatMessage(role="assistant", content=self._final_json(prompt, tool_message, context))

        # 采集类 Agent -> 先调用工具
        if "旅行信息采集专家" in prompt:
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="amap_search_poi", arguments={"keywords": "著名景点", "city": "北京", "limit": 5})],
            )
        if "住宿信息采集专家" in prompt:
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c2", name="amap_search_poi", arguments={"keywords": "酒店", "city": "北京", "limit": 5})],
            )
        if "餐饮信息采集专家" in prompt:
            context = self._context(messages)
            center = context.get("center") or {"longitude": 116.397, "latitude": 39.916}
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="c3",
                        name="amap_search_around",
                        arguments={
                            "longitude": center["longitude"],
                            "latitude": center["latitude"],
                            "keywords": "餐厅",
                            "radius": 2000,
                            "limit": 5,
                        },
                    )
                ],
            )

        # 规划类 Agent -> 直接输出 JSON
        return ChatMessage(role="assistant", content=self._final_json(prompt, None, context))

    # -- 结构化输出通道 ------------------------------------------------- #
    async def structured(self, messages, model_cls, temperature: float = 0.1):
        prompt = self._system_prompt(messages)
        context = self._context(messages)
        content = self._final_json(prompt, None, context)
        return model_cls.model_validate_json(content)

    async def validate_or_repair(self, messages, content, model_cls):
        return model_cls.model_validate_json(content)

    # -- 最终结果 ------------------------------------------------------- #
    def _final_json(self, prompt: str, tool_message: Optional[ChatMessage], context: Dict[str, Any]) -> str:
        if tool_message is not None:
            pois = json.loads(tool_message.content or "[]")
            if "住宿信息采集专家" in prompt:
                return json.dumps(
                    {
                        "pois": [
                            {
                                "id": p["id"],
                                "name": p["name"],
                                "address": p["address"],
                                "location": p["location"],
                                "rating": p["rating"],
                                "category": "酒店",
                                "description": "交通便利",
                                "price_range": "300-500元",
                                "estimated_cost": 400,
                            }
                            for p in pois
                        ]
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "pois": [
                        {
                            "id": p["id"],
                            "name": p["name"],
                            "address": p["address"],
                            "location": p["location"],
                            "rating": p["rating"],
                            "category": p.get("type", "景点"),
                            "description": f"{p['name']}值得一逛",
                            "visit_duration": 120,
                        }
                        for p in pois
                    ]
                },
                ensure_ascii=False,
            )

        if "行程规划专家" in prompt:
            attractions = context.get("attractions", [])
            hotels = context.get("hotels", [])
            days = []
            per_day = 2
            for index in range(int(context.get("travel_days", 2))):
                chunk = attractions[index * per_day : (index + 1) * per_day]
                days.append(
                    {
                        "day_index": index,
                        "description": f"第{index + 1}天行程",
                        "attraction_ids": [a["id"] for a in chunk],
                        "hotel_id": hotels[0]["id"] if hotels else None,
                        "notes": "",
                    }
                )
            return json.dumps(
                {"days": days, "overall_suggestions": "注意预约, 备好防晒"},
                ensure_ascii=False,
            )

        if "餐饮安排专家" in prompt:
            days = []
            for key, items in context.items():
                meals = []
                for offset, meal_type in enumerate(("breakfast", "lunch", "dinner")):
                    if offset >= len(items):
                        break
                    meals.append(
                        {
                            "meal_type": meal_type,
                            "restaurant_id": items[offset]["id"],
                            "estimated_cost": 50,
                            "description": "就近用餐",
                        }
                    )
                days.append({"day_index": int(key), "meals": meals})
            return json.dumps({"days": days}, ensure_ascii=False)

        return json.dumps({"pois": []}, ensure_ascii=False)


@pytest.fixture()
def fake_amap() -> FakeAmapClient:
    return FakeAmapClient()


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM()
