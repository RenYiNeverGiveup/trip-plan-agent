"""把高德地图能力包装成 Agent 可调用的工具。

这些工具是"事实数据入口": 行程里的经纬度、地址、票价、评分都来自这里,
LLM 只负责决定"去哪个、怎么排、怎么描述", 不负责编造事实。
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..registry import ToolRegistry, build_tool
from ...services.amap_service import AmapClient
from .mcp_amap import McpAmapProvider

logger = logging.getLogger(__name__)


async def build_amap_tools(
    client: AmapClient,
    mcp_provider: Optional[McpAmapProvider] = None,
) -> ToolRegistry:
    """构建高德地图工具集。

    Args:
        client: 高德 REST 客户端
        mcp_provider: 已连接的 MCP provider; 传入时工具来自 MCP server,
            否则使用本模块的 REST 直连实现
    """
    if mcp_provider is not None and mcp_provider.connected:
        try:
            specs = await mcp_provider.list_specs()
            if specs:
                logger.info("使用 MCP 高德工具: %s", [s.name for s in specs])
                return ToolRegistry(specs)
            logger.warning("MCP 未返回工具, 回落到 REST 直连")
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载 MCP 工具失败, 回落到 REST 直连: %s", exc)

    return _build_rest_tools(client)


def _build_rest_tools(client: AmapClient) -> ToolRegistry:
    """基于 REST 直连构建工具集。"""

    registry = ToolRegistry()

    async def search_poi(keywords: str, city: str, limit: int = 10) -> List[Dict[str, Any]]:
        """按关键字搜索城市内的兴趣点(景点/酒店/餐厅等)。

        Args:
            keywords: 搜索关键词, 例如 "故宫"、"博物馆"、"酒店"、"火锅"
            city: 城市名称, 例如 "北京"
            limit: 返回条数, 1-25
        """
        return await client.search_poi(keywords, city, limit=limit)

    async def poi_detail(poi_id: str) -> Dict[str, Any]:
        """查询 POI 详情, 返回门票价格、照片、评分等补充信息。

        Args:
            poi_id: POI 唯一 ID(来自搜索结果)
        """
        return await client.poi_detail(poi_id)

    async def weather(city: str) -> List[Dict[str, Any]]:
        """查询城市未来几天的天气预报(高德最多提供 4 天)。

        Args:
            city: 城市名称, 例如 "北京"
        """
        return await client.weather(city)

    async def search_around(
        longitude: float,
        latitude: float,
        keywords: str = "餐厅",
        radius: int = 2000,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """查询某坐标附近的兴趣点(常用于找景点附近的餐厅)。

        Args:
            longitude: 经度
            latitude: 纬度
            keywords: 关键词, 例如 "餐厅"、"咖啡馆"
            radius: 搜索半径(米), 最大 5000
            limit: 返回条数
        """
        return await client.search_around(longitude, latitude, keywords, radius, limit)

    async def route_distance(
        origin_longitude: float,
        origin_latitude: float,
        destination_longitude: float,
        destination_latitude: float,
        mode: str = "walking",
        city: str = "",
    ) -> Optional[Dict[str, Any]]:
        """查询两点之间的距离与耗时。

        Args:
            origin_longitude: 起点经度
            origin_latitude: 起点纬度
            destination_longitude: 终点经度
            destination_latitude: 终点纬度
            mode: walking(步行) / driving(驾车) / transit(公交地铁)
            city: 城市名称, 公交地铁路径规划时必填
        """
        origin: Tuple[float, float] = (origin_longitude, origin_latitude)
        destination: Tuple[float, float] = (destination_longitude, destination_latitude)
        return await client.route(origin, destination, mode=mode, city=city or None)

    registry.register_all(
        [
            build_tool("amap_search_poi", "搜索城市内的兴趣点", search_poi),
            build_tool("amap_poi_detail", "查询 POI 详情(票价/照片/评分)", poi_detail),
            build_tool("amap_weather", "查询城市天气预报", weather),
            build_tool("amap_search_around", "查询坐标附近的兴趣点", search_around),
            build_tool("amap_route_distance", "查询两点间距离与耗时", route_distance),
        ]
    )
    return registry
