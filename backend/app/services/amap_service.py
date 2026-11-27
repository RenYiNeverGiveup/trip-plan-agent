"""高德地图 Web 服务封装(直连 REST API)。

与原始实现相比:
1. 不再依赖 MCP server 进程(uvx amap-mcp-server), 去掉了一层外部依赖, `git clone` 即可运行;
2. 全部改为异步 httpx, 可与 LLM 调用并发;
3. 返回结构化数据(经纬度、票价、评分等), 这些事实性数据后续会直接进入行程, 不让模型编造。
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings
from ..logging_config import Timer, log_http_request, log_http_response

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


def _extract_temp(raw: Any) -> int:
    """提取温度(保留负号): "-1" -> -1, "10" -> 10。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    match = re.search(r"-?\d+(?:\.\d+)?", str(raw))
    return int(float(match.group())) if match else 0


def _to_https(url: Optional[str]) -> Optional[str]:
    """把图片地址升级为 https。

    高德返回的配图有一半是 http://, 在 https 页面里会被浏览器按"混合内容"拦截,
    导致图片不显示, 这里统一升级为 https。
    """
    if not url:
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


class AmapBusinessError(RuntimeError):
    """高德返回的业务错误(status != 1), 例如权限不足、参数非法、密钥无效。

    这类错误重试没有意义, 因此与网络错误区分开, 不参与重试。
    """


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
            event_hooks={"request": [self._on_request], "response": [self._on_response]},
        )

    # ------------------------------------------------------------------ #
    # 第三方调用日志(httpx 事件钩子, 所有请求一处覆盖)
    # ------------------------------------------------------------------ #
    async def _on_request(self, request: httpx.Request) -> None:
        """打印请求入参(含参数, key 已脱敏)。

        注意: httpx 的 AsyncClient 会 `await hook(...)`, 因此钩子必须是协程函数。
        """
        request.extensions["_start_time"] = time.perf_counter()
        params = dict(request.url.params)
        log_http_request(logger, request.method, str(request.url), params)

    async def _on_response(self, response: httpx.Response) -> None:
        """打印响应出参(状态码、耗时、响应体摘要)。"""
        start = response.request.extensions.get("_start_time")
        elapsed_ms = (time.perf_counter() - start) * 1000 if start else None
        body: Any = None
        try:
            # 响应体可能尚未读取, 先读取再解析
            await response.aread()
            body = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应退化为文本
            try:
                body = response.text
            except Exception:  # noqa: BLE001
                body = None
        log_http_response(
            logger,
            response.request.method,
            str(response.request.url),
            response.status_code,
            body,
            elapsed_ms,
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
                    # 业务错误(权限不足 / 参数错误 / 配额超限)重试没有意义, 直接抛出
                    raise AmapBusinessError(
                        f"高德接口 {path} 返回失败: {payload.get('info', '未知错误')} "
                        f"(infocode={payload.get('infocode', '-')})"
                    )
                return payload
            except AmapBusinessError:
                raise
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
        detail["photos"] = [_to_https(p.get("url")) for p in (poi.get("photos") or []) if p.get("url")]
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
        """天气预报。高德最多返回 4 天(今天起)。

        注意: 高德返回的逐日数组字段名是 `casts`(不是 `forecast`)。
        """
        payload = await self._get("/weather/weatherInfo", {"city": city, "extensions": "all"})
        forecasts = payload.get("forecasts") or []
        if not forecasts:
            logger.warning("高德天气返回为空: city=%s payload=%s", city, list(payload.keys()))
            return []
        # 高德实际字段为 casts, 兼容个别版本返回 forecast
        days = forecasts[0].get("casts") or forecasts[0].get("forecast") or []
        if not days:
            logger.warning("高德天气未返回逐日预报: city=%s", city)
            return []
        result: List[Dict[str, Any]] = []
        for day in days:
            result.append(
                {
                    "date": day.get("date", ""),
                    "week": day.get("week", ""),
                    "day_weather": day.get("dayweather", ""),
                    "night_weather": day.get("nightweather", ""),
                    "day_temp": _extract_temp(day.get("daytemp")),
                    "night_temp": _extract_temp(day.get("nighttemp")),
                    "wind_direction": day.get("daywind", ""),
                    "wind_power": day.get("daypower", ""),
                    "night_wind_direction": day.get("nightwind", ""),
                    "night_wind_power": day.get("nightpower", ""),
                }
            )
        logger.info("高德天气解析成功: city=%s 天数=%s", city, len(result))
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
        origin_poi_id: Optional[str] = None,
        destination_poi_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """两点间路径规划, 返回距离(米)与耗时(秒)。失败返回 None。

        按高德 Web 服务文档(https://lbs.amap.com/api/webservice/guide/api/direction):
        - 步行: `/direction/walking`
        - 驾车: `/direction/driving` —— `extensions` 必填, `strategy` 建议 10(躲避拥堵且路程较短)
        - 公交: `/direction/transit/integrated` —— `city` 必填(城市名或 citycode),
          `cityd` 仅跨城规划时必填
        """
        origin_str = f"{origin[0]:.6f},{origin[1]:.6f}"
        destination_str = f"{destination[0]:.6f},{destination[1]:.6f}"
        params: Dict[str, Any] = {"origin": origin_str, "destination": destination_str}
        # 已知 POI ID 时带上, 可以显著提升路径匹配准确度
        if origin_poi_id:
            params["origin_id"] = origin_poi_id
        if destination_poi_id:
            params["destination_id"] = destination_poi_id

        try:
            if mode == "driving":
                payload = await self._get(
                    "/direction/driving",
                    {**params, "extensions": "base", "strategy": 10},
                )
            elif mode == "transit":
                if not city:
                    logger.warning("公交路径规划缺少 city 参数, 已跳过")
                    return None
                payload = await self._get(
                    "/direction/transit/integrated",
                    {**params, "city": city, "extensions": "base", "strategy": 0},
                )
            else:
                payload = await self._get("/direction/walking", params)
        except Exception as exc:  # noqa: BLE001 - 路径规划失败时退回直线距离估算
            logger.warning("路径规划失败(mode=%s): %s", mode, exc)
            return None

        route = payload.get("route") or {}
        if mode == "transit":
            return self._parse_transit(route, mode)

        paths = route.get("paths") or []
        if not paths:
            return None
        path = paths[0]
        return {
            "distance_m": int(path.get("distance") or 0),
            "duration_s": int(path.get("duration") or 0),
            "mode": mode,
        }

    @staticmethod
    def _parse_transit(route: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
        """解析公交路径规划结果。

        `/direction/transit/integrated` 返回的是 `route.transits[]`,
        每一段方案包含 duration/cost 与 segments[], 距离需要自行累加
        (segments 里的 walking / bus / railway 各自带 distance)。
        """
        transits = route.get("transits") or []
        if not transits:
            return None

        best: Optional[Dict[str, Any]] = None
        for transit in transits:
            distance = 0
            for segment in transit.get("segments") or []:
                for part in ("walking", "bus", "railway"):
                    distance += int((segment.get(part) or {}).get("distance") or 0)
            duration = int(transit.get("duration") or 0)
            if best is None or duration < best["duration_s"]:
                best = {
                    "distance_m": distance,
                    "duration_s": duration,
                    "mode": mode,
                }

        if best and not best["distance_m"]:
            # 兜底: 某些响应把总距离放在 route.distance
            best["distance_m"] = int(route.get("distance") or 0)
        return best


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
