"""高德地图 Web 服务封装(直连 REST API)。

与原始实现相比:
1. 不再依赖 MCP server 进程(uvx amap-mcp-server), 去掉了一层外部依赖, `git clone` 即可运行;
2. 全部改为异步 httpx, 可与 LLM 调用并发;
3. 返回结构化数据(经纬度、票价、评分等), 这些事实性数据后续会直接进入行程, 不让模型编造。
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_AMAP_BASE_URL = "https://restapi.amap.com/v3"


def _parse_location(raw: str) -> Optional[Dict[str, float]]:
    """把高德的 "116.397128,39.916527" 解析为经纬度字典。"""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) < 2:
        return None
    try:
        return {"longitude": float(parts[0]), "latitude": float(parts[1])}
    except ValueError:
        return None


def _extract_price(raw: Any) -> int:
    """从 "60元起" / "60.00" / 60 中提取整数价格(元)。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    match = re.search(r"\d+(?:\.\d+)?", str(raw))
    return int(float(match.group())) if match else 0


class AmapClient:
    """高德地图 Web 服务客户端(异步)。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = _AMAP_BASE_URL,
        timeout: float = 15.0,
        pool_size: int = 20,
    ) -> None:
        if not api_key:
            raise ValueError("高德地图 API Key 未配置, 请在 .env 中设置 AMAP_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "trip-plan-agent/1.0"},
            limits=httpx.Limits(max_connections=pool_size),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = {**params, "key": self.api_key, "output": "json"}
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") != "1":
                    raise RuntimeError(f"高德接口 {path} 返回失败: {payload.get('info', '未知错误')}")
                return payload
            except Exception as exc:  # noqa: BLE001 - 网络类错误统一重试
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"请求高德接口 {path} 失败: {last_error}")

    # ------------------------------------------------------------------ #
    # POI
    # ------------------------------------------------------------------ #
    async def search_poi(
        self,
        keywords: str,
        city: str,
        limit: int = 10,
        citylimit: bool = True,
    ) -> List[Dict[str, Any]]:
        """关键字搜索 POI。"""
        payload = await self._get(
            "/place/text",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": str(citylimit).lower(),
                "offset": min(max(limit, 1), 25),
                "page": 1,
                "extensions": "all",
            },
        )
        return [self._normalize_poi(p) for p in payload.get("pois", [])]

    async def search_around(
        self,
        longitude: float,
        latitude: float,
        keywords: str = "",
        radius: int = 3000,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        """周边 POI 搜索(用于寻找景点附近的餐厅)。"""
        payload = await self._get(
            "/place/around",
            {
                "location": f"{longitude},{latitude}",
                "keywords": keywords,
                "radius": radius,
                "offset": min(max(limit, 1), 25),
                "page": 1,
                "extensions": "all",
            },
        )
        return [self._normalize_poi(p) for p in payload.get("pois", [])]

    async def poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """POI 详情(照片、票价等)。"""
        payload = await self._get("/place/detail", {"id": poi_id, "extensions": "all"})
        pois = payload.get("pois") or []
        if not pois:
            raise RuntimeError(f"未找到 POI: {poi_id}")
        poi = pois[0]
        detail = self._normalize_poi(poi)
        biz_ext = poi.get("biz_ext") or {}
        detail["ticket_price"] = _extract_price(biz_ext.get("ticket_price")) or _extract_price(poi.get("cost"))
        detail["photos"] = [p.get("url") for p in (poi.get("photos") or []) if p.get("url")]
        detail["business_area"] = poi.get("business_area")
        detail["tag"] = poi.get("tag") or biz_ext.get("tag")
        return detail

    @staticmethod
    def _normalize_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
        location = _parse_location(poi.get("location", "")) or {"longitude": 0.0, "latitude": 0.0}
        biz_ext = poi.get("biz_ext") or {}
        rating_raw = poi.get("rating") or biz_ext.get("rating")
        try:
            rating = float(rating_raw) if rating_raw not in (None, "", []) else None
        except (TypeError, ValueError):
            rating = None
        return {
            "id": poi.get("id", ""),
            "name": poi.get("name", ""),
            "address": poi.get("address", "") or poi.get("business_area", "") or "",
            "location": location,
            "type": poi.get("type", ""),
            "typecode": poi.get("typecode", ""),
            "tel": poi.get("tel", ""),
            "rating": rating,
            "cost": _extract_price(poi.get("cost")) or None,
            "tag": poi.get("tag") or biz_ext.get("tag"),
        }

    # ------------------------------------------------------------------ #
    # 天气
    # ------------------------------------------------------------------ #
    async def weather(self, city: str) -> List[Dict[str, Any]]:
        """天气预报。高德最多返回 4 天(今天起)。"""
        payload = await self._get("/weather/weatherInfo", {"city": city, "extensions": "all"})
        forecasts = payload.get("forecasts") or []
        if not forecasts:
            return []
        days = forecasts[0].get("forecast") or []
        result: List[Dict[str, Any]] = []
        for day in days:
            result.append(
                {
                    "date": day.get("date", ""),
                    "week": day.get("week", ""),
                    "day_weather": day.get("dayweather", ""),
                    "night_weather": day.get("nightweather", ""),
                    "day_temp": _extract_price(day.get("daytemp")),
                    "night_temp": _extract_price(day.get("nighttemp")),
                    "wind_direction": day.get("daywind", ""),
                    "wind_power": day.get("daypower", ""),
                    "night_wind_direction": day.get("nightwind", ""),
                    "night_wind_power": day.get("nightpower", ""),
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # 地理编码
    # ------------------------------------------------------------------ #
    async def geocode(self, address: str, city: Optional[str] = None) -> Optional[Dict[str, float]]:
        """地址 -> 经纬度。"""
        params: Dict[str, Any] = {"address": address}
        if city:
            params["city"] = city
        payload = await self._get("/geocode/geo", params)
        geocodes = payload.get("geocodes") or []
        if not geocodes:
            return None
        return _parse_location(geocodes[0].get("location", ""))

    # ------------------------------------------------------------------ #
    # 路径规划
    # ------------------------------------------------------------------ #
    async def route(
        self,
        origin: Tuple[float, float],
        destination: Tuple[float, float],
        mode: str = "walking",
        city: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """两点间路径规划, 返回距离(米)与耗时(秒)。失败返回 None。"""
        origin_str = f"{origin[0]},{origin[1]}"
        destination_str = f"{destination[0]},{destination[1]}"
        try:
            if mode == "driving":
                payload = await self._get(
                    "/direction/driving",
                    {"origin": origin_str, "destination": destination_str, "strategy": 0},
                )
            elif mode == "transit":
                if not city:
                    return None
                payload = await self._get(
                    "/direction/transit",
                    {"origin": origin_str, "destination": destination_str, "city": city, "cityd": city},
                )
            else:
                payload = await self._get(
                    "/direction/walking",
                    {"origin": origin_str, "destination": destination_str},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("路径规划失败(mode=%s): %s", mode, exc)
            return None

        paths = (payload.get("route") or {}).get("paths") or []
        if not paths:
            return None
        path = paths[0]
        return {
            "distance_m": int(path.get("distance") or 0),
            "duration_s": int(path.get("duration") or 0),
            "mode": mode,
        }


# ---------------------------------------------------------------------- #
# 单例
# ---------------------------------------------------------------------- #
_amap_client: Optional[AmapClient] = None


def get_amap_client() -> AmapClient:
    """获取高德客户端(单例)。"""
    global _amap_client
    if _amap_client is None:
        settings = get_settings()
        _amap_client = AmapClient(
            api_key=settings.amap_api_key,
            base_url=settings.amap_base_url,
            timeout=settings.amap_timeout,
        )
    return _amap_client


class AmapService:
    """兼容原有调用方式的服务门面(全部为异步)。"""

    def __init__(self) -> None:
        self.client = get_amap_client()

    async def search_poi(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        return await self.client.search_poi(keywords, city, limit=limit, citylimit=citylimit)

    async def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        return await self.client.poi_detail(poi_id)

    async def get_weather(self, city: str) -> List[Dict[str, Any]]:
        return await self.client.weather(city)

    async def plan_route(
        self,
        origin: Tuple[float, float],
        destination: Tuple[float, float],
        route_type: str = "walking",
        city: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self.client.route(origin, destination, mode=route_type, city=city)

    async def plan_route_by_address(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Optional[Dict[str, Any]]:
        """地址版路径规划(自动地理编码)。"""
        origin = await self.client.geocode(origin_address, origin_city)
        destination = await self.client.geocode(destination_address, destination_city)
        if not origin or not destination:
            return None
        return await self.client.route(
            (origin["longitude"], origin["latitude"]),
            (destination["longitude"], destination["latitude"]),
            mode=route_type,
            city=destination_city or origin_city,
        )


_amap_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    """获取 AmapService(单例)。"""
    global _amap_service
    if _amap_service is None:
        _amap_service = AmapService()
    return _amap_service
