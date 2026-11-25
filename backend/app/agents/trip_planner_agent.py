"""多智能体旅行规划系统(基于自研 Agent 框架)。

整体流水线:
    阶段A(并行): 景点采集 Agent / 住宿采集 Agent / 天气查询
    阶段B      : 规划 Agent 从真实候选中挑选每天的景点与酒店
    阶段C(并行): 餐饮采集 Agent(按天, 周边检索) -> 餐饮规划
    阶段D      : POI 详情补全 -> 路线优化(最近邻+2-opt) -> 交通测算 -> 预算汇总

核心原则: **事实来自高德 API, 判断留给模型**。
模型只负责"选哪些、去哪吃、怎么描述", 经纬度、地址、门票、距离、费用全部由
工具返回或算法计算, 因此结果可复现、可校验, 不会出现"编造的坐标"。
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..agent.executor import Agent
from ..agent.llm import LLMClient
from ..agent.orchestrator import run_parallel
from ..agent.registry import ToolRegistry
from ..agent.toolkits.amap_tools import build_amap_tools
from ..agent.toolkits.mcp_amap import McpAmapProvider
from ..agent.utils import safe_int
from ..config import get_settings
from ..models.schemas import (
    Attraction,
    DayPlan,
    Hotel,
    Location,
    Meal,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..planning.budget import compute_budget
from ..planning.geo import centroid, haversine_km
from ..planning.route import optimize_route
from ..planning.transport import estimate_cost, estimate_duration_min, resolve_mode
from ..services.amap_service import AmapClient, get_amap_client
from ..services.llm_service import get_llm
from .llm_schemas import CandidateList, DayMealPlan, DaySelection, MealPlan, PlanSelection
from .poi_store import POIStore
from .prompts import (
    ATTRACTION_COLLECTOR_PROMPT,
    HOTEL_COLLECTOR_PROMPT,
    MEAL_COLLECTOR_PROMPT,
    MEAL_PLANNER_PROMPT,
    PLANNER_PROMPT,
)

logger = logging.getLogger(__name__)

# 直线距离 -> 实际路网的经验绕行系数
DETOUR_FACTOR = 1.3


class TripPlanningError(RuntimeError):
    """行程生成失败。"""


class MultiAgentTripPlanner:
    """多智能体旅行规划系统。"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        amap_client: Optional[AmapClient] = None,
    ) -> None:
        self.settings = get_settings()
        self.llm = llm or get_llm()
        self.amap = amap_client or get_amap_client()
        self.store = POIStore()
        self.mcp_provider: Optional[McpAmapProvider] = None
        self.tools = ToolRegistry()
        self._tools_ready = False

    # ------------------------------------------------------------------ #
    # 工具与记录
    # ------------------------------------------------------------------ #
    async def ensure_tools(self) -> ToolRegistry:
        """按配置初始化工具集(MCP 优先, 失败回落 REST)。"""
        if self._tools_ready:
            return self.tools

        provider = None
        if self.settings.use_mcp_amap:
            provider = McpAmapProvider(
                command=self.settings.mcp_amap_command.split(),
                env={"AMAP_MAPS_API_KEY": self.settings.amap_api_key},
                timeout=self.settings.mcp_amap_timeout,
            )
            if await provider.start():
                self.mcp_provider = provider
            else:
                provider = None

        registry = await build_amap_tools(self.amap, provider)

        for spec in registry.specs():
            original = spec.handler

            async def wrapper(*args, _original=original, **kwargs):
                result = await _original(*args, **kwargs)
                self._record(result)
                return result

            spec.handler = wrapper

        self.tools = registry
        self._tools_ready = True
        return self.tools

    async def close(self) -> None:
        """释放 MCP 子进程等资源。"""
        if self.mcp_provider is not None:
            await self.mcp_provider.close()
            self.mcp_provider = None
        self._tools_ready = False

    def _record(self, result: Any) -> None:
        """把工具返回的 POI 登记进仓库。"""
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    self.store.add(item)
        elif isinstance(result, dict):
            self.store.add(result)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def plan_trip(self, request: TripRequest) -> TripPlan:
        """生成旅行计划。"""
        await self.ensure_tools()

        logger.info(
            "开始规划: %s %s-%s (%s天)",
            request.city,
            request.start_date,
            request.end_date,
            request.travel_days,
        )

        # ---------- 阶段A: 并行采集 ----------
        results = await run_parallel(
            {
                "attractions": lambda: self._collect_attractions(request),
                "hotels": lambda: self._collect_hotels(request),
                "weather": lambda: self._fetch_weather(request.city),
            }
        )

        attractions: List[Dict[str, Any]] = results.get("attractions") or []
        hotels: List[Dict[str, Any]] = results.get("hotels") or []
        weather: List[Dict[str, Any]] = results.get("weather") or []

        if not attractions:
            raise TripPlanningError(
                "未能获取到任何景点数据, 请检查 AMAP_API_KEY 与网络连通性"
            )

        # ---------- 阶段B: 规划选择 ----------
        selection = await self._plan_days(request, attractions, hotels, weather)

        # ---------- 阶段C/D: 组装每日行程 ----------
        days = await self._build_days(request, selection, attractions, hotels)

        dates = self._build_dates(request)
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=self._build_weather_info(dates, weather),
            overall_suggestions=selection.overall_suggestions
            or "行程基于高德地图真实 POI 生成, 出发前请再次确认开放时间与票价。",
            budget=compute_budget(days),
        )
        logger.info("行程生成完成: %s", request.city)
        return plan

    @staticmethod
    def _build_dates(request: TripRequest) -> List[str]:
        start = datetime.strptime(request.start_date, "%Y-%m-%d")
        return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(request.travel_days)]

    @staticmethod
    def _preference_keywords(request: TripRequest) -> str:
        if request.preferences:
            return "、".join(request.preferences)
        return "热门景点"

    # ------------------------------------------------------------------ #
    # 阶段A: 采集
    # ------------------------------------------------------------------ #
    async def _collect_attractions(self, request: TripRequest) -> List[Dict[str, Any]]:
        prompt = ATTRACTION_COLLECTOR_PROMPT.format(
            city=request.city,
            preference_keywords=self._preference_keywords(request),
        )
        agent = Agent(
            name="景点采集专家",
            system_prompt=prompt,
            llm=self.llm,
            tools=self.tools,
            max_steps=max(self.settings.llm_max_steps, 8),
            temperature=0.1,
        )
        try:
            result = await agent.run_structured("请开始采集候选景点。", CandidateList)
        except Exception as exc:  # noqa: BLE001
            logger.warning("景点采集 Agent 失败, 降级为直接检索: %s", exc)
            return await self._fallback_search(request, ["著名景点", "博物馆", "公园", "古镇"])
        return self._resolve_candidates(result.pois)

    async def _collect_hotels(self, request: TripRequest) -> List[Dict[str, Any]]:
        prompt = HOTEL_COLLECTOR_PROMPT.format(
            city=request.city,
            accommodation=request.accommodation or "酒店",
        )
        agent = Agent(
            name="住宿采集专家",
            system_prompt=prompt,
            llm=self.llm,
            tools=self.tools,
            max_steps=max(self.settings.llm_max_steps, 6),
            temperature=0.1,
        )
        try:
            result = await agent.run_structured("请开始采集候选酒店。", CandidateList)
        except Exception as exc:  # noqa: BLE001
            logger.warning("住宿采集 Agent 失败, 降级为直接检索: %s", exc)
            return await self._fallback_search(request, ["酒店"])
        return self._resolve_candidates(result.pois)

    async def _fallback_search(
        self, request: TripRequest, keywords_list: List[str]
    ) -> List[Dict[str, Any]]:
        """模型不可用时的确定性检索(直接调用高德接口)。"""
        collected: List[Dict[str, Any]] = []
        seen: set = set()
        for keywords in keywords_list:
            try:
                pois = await self.amap.search_poi(keywords, request.city, limit=8)
            except Exception as exc:  # noqa: BLE001
                logger.warning("检索 %s 失败: %s", keywords, exc)
                continue
            for poi in pois or []:
                poi_id = str(poi.get("id") or "")
                if not poi_id or poi_id in seen:
                    continue
                seen.add(poi_id)
                self.store.add(poi)
                collected.append(dict(poi))
        return collected

    async def _fetch_weather(self, city: str) -> List[Dict[str, Any]]:
        """天气属于纯事实数据, 直接查接口, 不经过模型。"""
        try:
            return await self.amap.weather(city)
        except Exception as exc:  # noqa: BLE001
            logger.warning("天气查询失败: %s", exc)
            return []

    def _resolve_candidates(self, candidates: List[Any]) -> List[Dict[str, Any]]:
        """把模型给出的候选映射回高德原始数据。"""
        resolved: List[Dict[str, Any]] = []
        seen: set = set()

        for candidate in candidates:
            data = candidate.model_dump() if hasattr(candidate, "model_dump") else dict(candidate)
            poi = self.store.get(data.get("id", "")) or self.store.find_by_name(data.get("name", ""))
            if not poi:
                # 模型编造了不存在的 POI, 直接丢弃
                logger.warning("丢弃无法溯源的候选: %s", data.get("name"))
                continue

            poi_id = str(poi.get("id") or data.get("id") or "")
            if poi_id in seen:
                continue
            seen.add(poi_id)

            item = dict(poi)
            item["id"] = poi_id
            item["description"] = data.get("description") or poi.get("description", "")
            item["category"] = data.get("category") or poi.get("type") or poi.get("category", "")
            item["visit_duration"] = safe_int(data.get("visit_duration"), 120) or 120
            item["ticket_price"] = safe_int(poi.get("ticket_price")) or safe_int(data.get("ticket_price"))
            item["estimated_cost"] = safe_int(poi.get("cost")) or safe_int(data.get("estimated_cost"))
            item["price_range"] = data.get("price_range") or poi.get("price_range", "")
            if poi.get("rating") is not None:
                item["rating"] = poi.get("rating")
            resolved.append(item)

        return resolved

    # ------------------------------------------------------------------ #
    # 阶段B: 规划
    # ------------------------------------------------------------------ #
    async def _plan_days(
        self,
        request: TripRequest,
        attractions: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
        weather: List[Dict[str, Any]],
    ) -> PlanSelection:
        prompt = PLANNER_PROMPT.format(
            city=request.city,
            days=request.travel_days,
            preferences=self._preference_keywords(request),
            transportation=request.transportation,
        )
        payload = {
            "attractions": [self._catalog_entry(a) for a in attractions],
            "hotels": [self._catalog_entry(h) for h in hotels],
            "weather": weather,
            "start_date": request.start_date,
            "travel_days": request.travel_days,
            "extra_requirements": request.free_text_input,
        }
        agent = Agent(
            name="行程规划专家",
            system_prompt=prompt,
            llm=self.llm,
            tools=None,
            max_steps=2,
            temperature=0.2,
        )
        try:
            return await agent.run_structured(
                "请根据下面的候选数据规划行程。",
                PlanSelection,
                context=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("规划 Agent 失败, 降级为确定性排程: %s", exc)
            return self._fallback_selection(request, attractions, hotels)

    @staticmethod
    def _catalog_entry(item: Dict[str, Any]) -> Dict[str, Any]:
        """送给模型看的候选条目(去掉冗余字段, 控制 prompt 体积)。"""
        return {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "category": item.get("category", ""),
            "rating": item.get("rating"),
            "ticket_price": item.get("ticket_price", 0),
            "estimated_cost": item.get("estimated_cost", 0),
            "price_range": item.get("price_range", ""),
            "address": item.get("address", ""),
            "description": (item.get("description") or "")[:80],
            "visit_duration": item.get("visit_duration", 120),
            "location": item.get("location"),
        }

    def _fallback_selection(
        self,
        request: TripRequest,
        attractions: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
    ) -> PlanSelection:
        """模型不可用时的确定性排程(仍然只用真实 POI)。"""
        pool = sorted(attractions, key=lambda a: (a.get("rating") or 0), reverse=True)
        per_day = max(2, min(self.settings.attractions_per_day, len(pool) or 1))
        selections: List[DaySelection] = []
        for index in range(request.travel_days):
            chunk = pool[index * per_day : (index + 1) * per_day] or pool[:per_day]
            selections.append(
                DaySelection(
                    day_index=index,
                    description=f"第{index + 1}天: 按评分与距离自动排布的精选景点",
                    attraction_ids=[a["id"] for a in chunk],
                    hotel_id=hotels[0]["id"] if hotels else None,
                    notes="模型不可用, 本日行程由算法按评分与距离自动生成, 建议人工复核",
                )
            )
        return PlanSelection(
            days=selections,
            overall_suggestions="当前为降级模式: 行程由高德真实 POI 与评分自动生成, 未包含模型撰写的个性化建议, 建议人工复核。",
        )

    # ------------------------------------------------------------------ #
    # 阶段C/D: 组装
    # ------------------------------------------------------------------ #
    async def _build_days(
        self,
        request: TripRequest,
        selection: PlanSelection,
        attractions: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
    ) -> List[DayPlan]:
        dates = self._build_dates(request)
        mode = resolve_mode(request.transportation)

        attraction_map = {a["id"]: a for a in attractions}
        hotel_map = {h["id"]: h for h in hotels}

        # 补齐景点详情(门票、照片)
        selected_ids = [pid for day in selection.days for pid in day.attraction_ids]
        await self._enrich_details(selected_ids)
        # 详情是后补的, 需要刷新一次候选数据
        for pid, item in list(attraction_map.items()):
            enriched = self.store.get(pid)
            if enriched:
                attraction_map[pid] = {**item, **enriched}

        # 餐饮: 按天并行采集周边餐厅, 再统一规划
        meal_candidates = await self._collect_meals(request, selection, attraction_map)
        meal_plan = await self._plan_meals(selection, meal_candidates)
        meal_map = {day.day_index: day for day in meal_plan.days}

        days: List[DayPlan] = []
        for day_index, day_selection in enumerate(selection.days):
            items = [attraction_map[pid] for pid in day_selection.attraction_ids if pid in attraction_map]
            if not items:
                items = list(attractions[:2])
            hotel = hotel_map.get(day_selection.hotel_id or "")

            start_location = hotel.get("location") if hotel else None
            route = optimize_route(items, start_location)
            ordered = [items[i] for i in route["order"]] or items

            distance_km, duration_min = await self._measure_legs(ordered, start_location, mode, request.city)
            cost = estimate_cost(mode, distance_km)

            day_meals = self._build_meals(meal_map.get(day_index), meal_candidates.get(day_index, []))
            day_plan = DayPlan(
                date=dates[day_index] if day_index < len(dates) else request.start_date,
                day_index=day_index,
                description=day_selection.description or f"第{day_index + 1}天行程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                hotel=self._build_hotel(hotel, ordered),
                attractions=[self._build_attraction(a) for a in ordered],
                meals=day_meals,
                transport_distance_km=round(distance_km, 2),
                transport_cost=cost,
                transport_duration_min=duration_min,
                route_summary=" → ".join(a.get("name", "") for a in ordered),
            )
            days.append(day_plan)

        return days

    async def _enrich_details(self, poi_ids: List[str]) -> None:
        """并发补齐景点详情(门票、照片)。"""
        unique = list(dict.fromkeys([pid for pid in poi_ids if pid]))
        if not unique:
            return

        async def fetch(poi_id: str) -> Optional[Dict[str, Any]]:
            try:
                return await self.amap.poi_detail(poi_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 POI 详情失败 %s: %s", poi_id, exc)
                return None

        details = await asyncio.gather(*(fetch(pid) for pid in unique))
        for detail in details:
            if not detail:
                continue
            stored = self.store.get(detail.get("id", ""))
            if stored is not None:
                self.store.add(detail)

    async def _collect_meals(
        self,
        request: TripRequest,
        selection: PlanSelection,
        attraction_map: Dict[str, Dict[str, Any]],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """按天并行检索周边餐厅。"""
        tasks: Dict[str, Any] = {}
        for day in selection.days:
            items = [attraction_map[pid] for pid in day.attraction_ids if pid in attraction_map]
            locations = [item.get("location") for item in items if item.get("location")]
            center = centroid(locations) if locations else None
            if not center:
                continue
            tasks[str(day.day_index)] = self._collect_meals_for_day(day.day_index, center)

        if not tasks:
            return {}

        results = await run_parallel(tasks)
        candidates: Dict[int, List[Dict[str, Any]]] = {}
        for key, value in results.items():
            if isinstance(value, BaseException):
                continue
            candidates[int(key)] = value
        return candidates

    async def _collect_meals_for_day(
        self, day_index: int, center: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        prompt = MEAL_COLLECTOR_PROMPT.format(day_index=day_index + 1, radius=2000)
        agent = Agent(
            name=f"餐饮采集专家-第{day_index + 1}天",
            system_prompt=prompt,
            llm=self.llm,
            tools=self.tools,
            max_steps=4,
            temperature=0.1,
        )
        try:
            result = await agent.run_structured(
                "请采集当天的候选餐厅。",
                CandidateList,
                context={"center": center},
            )
            return self._resolve_candidates(result.pois)
        except Exception as exc:  # noqa: BLE001
            logger.warning("餐饮采集失败(第%s天), 降级为直接检索: %s", day_index + 1, exc)
            try:
                pois = await self.amap.search_around(
                    center["longitude"], center["latitude"], "餐厅", 2000, 10
                )
            except Exception as inner_exc:  # noqa: BLE001
                logger.warning("餐厅检索失败: %s", inner_exc)
                return []
            for poi in pois or []:
                self.store.add(poi)
            return [dict(poi) for poi in pois or []]

    async def _plan_meals(
        self,
        selection: PlanSelection,
        candidates: Dict[int, List[Dict[str, Any]]],
    ) -> MealPlan:
        payload = {
            str(day_index): [self._catalog_entry(item) for item in items]
            for day_index, items in candidates.items()
        }
        agent = Agent(
            name="餐饮规划专家",
            system_prompt=MEAL_PLANNER_PROMPT,
            llm=self.llm,
            tools=None,
            max_steps=2,
            temperature=0.2,
        )
        try:
            return await agent.run_structured("请为每天挑选餐厅。", MealPlan, context=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("餐饮规划失败, 降级为顺序选取: %s", exc)
            return self._fallback_meals(candidates)

    @staticmethod
    def _fallback_meals(candidates: Dict[int, List[Dict[str, Any]]]) -> MealPlan:
        days: List[DayMealPlan] = []
        for day_index, items in candidates.items():
            meals = []
            for offset, meal_type in enumerate(("breakfast", "lunch", "dinner")):
                if offset >= len(items):
                    break
                item = items[offset]
                meals.append(
                    {
                        "meal_type": meal_type,
                        "restaurant_id": item.get("id", ""),
                        "estimated_cost": safe_int(item.get("estimated_cost"), 50),
                        "description": item.get("description", ""),
                    }
                )
            days.append(DayMealPlan(day_index=day_index, meals=meals))
        return MealPlan(days=days)

    def _build_meals(
        self,
        meal_plan: Optional[DayMealPlan],
        candidates: List[Dict[str, Any]],
    ) -> List[Meal]:
        """把餐饮选择映射回真实餐厅 POI。"""
        candidate_map = {item["id"]: item for item in candidates}
        meals: List[Meal] = []
        for choice in (meal_plan.meals if meal_plan else []):
            restaurant = candidate_map.get(choice.restaurant_id) or self.store.get(choice.restaurant_id)
            if not restaurant:
                continue
            meals.append(
                Meal(
                    type=choice.meal_type,
                    name=restaurant.get("name", ""),
                    address=restaurant.get("address", ""),
                    location=self._location(restaurant.get("location")),
                    description=choice.description or restaurant.get("description", ""),
                    estimated_cost=safe_int(choice.estimated_cost)
                    or safe_int(restaurant.get("estimated_cost"), 50),
                )
            )
        return meals

    @staticmethod
    def _build_attraction(item: Dict[str, Any]) -> Attraction:
        photos = item.get("photos") or []
        return Attraction(
            name=item.get("name", ""),
            address=item.get("address", ""),
            location=MultiAgentTripPlanner._location(item.get("location")),
            visit_duration=safe_int(item.get("visit_duration"), 120),
            description=item.get("description", ""),
            category=item.get("category", ""),
            rating=item.get("rating"),
            photos=photos,
            poi_id=item.get("id", ""),
            image_url=photos[0] if photos else None,
            ticket_price=safe_int(item.get("ticket_price")),
        )

    def _build_hotel(
        self,
        hotel: Optional[Dict[str, Any]],
        attractions: List[Dict[str, Any]],
    ) -> Optional[Hotel]:
        if not hotel:
            return None
        distance_text = ""
        if attractions and attractions[0].get("location") and hotel.get("location"):
            km = haversine_km(hotel["location"], attractions[0]["location"])
            distance_text = f"距离首个景点约{km:.1f}公里"
        rating = hotel.get("rating")
        return Hotel(
            name=hotel.get("name", ""),
            address=hotel.get("address", ""),
            location=self._location(hotel.get("location")),
            price_range=hotel.get("price_range", ""),
            rating=f"{rating}" if rating is not None else "",
            distance=distance_text,
            type=hotel.get("category") or "酒店",
            estimated_cost=safe_int(hotel.get("estimated_cost")),
        )

    @staticmethod
    def _location(raw: Optional[Dict[str, Any]]) -> Location:
        if not raw:
            return Location(longitude=0.0, latitude=0.0)
        return Location(
            longitude=float(raw.get("longitude") or 0.0),
            latitude=float(raw.get("latitude") or 0.0),
        )

    # ------------------------------------------------------------------ #
    # 交通测算
    # ------------------------------------------------------------------ #
    async def _measure_legs(
        self,
        attractions: List[Dict[str, Any]],
        start: Optional[Dict[str, Any]],
        mode: str,
        city: str,
    ) -> Tuple[float, int]:
        """测量当天各段距离与耗时(优先使用高德路径规划)。"""
        path: List[Tuple[float, float]] = []
        if start:
            path.append((float(start["longitude"]), float(start["latitude"])))
        for item in attractions:
            location = item.get("location") or {}
            path.append(
                (float(location.get("longitude") or 0.0), float(location.get("latitude") or 0.0))
            )
        if start and len(path) > 1:
            path.append(path[0])  # 返回住处

        total_km = 0.0
        total_min = 0.0

        for index in range(len(path) - 1):
            origin, destination = path[index], path[index + 1]
            result = await self.amap.route(origin, destination, mode=mode, city=city)
            if result and result.get("distance_m"):
                total_km += result["distance_m"] / 1000.0
                total_min += result.get("duration_s", 0) / 60.0
            else:
                straight = haversine_km(
                    {"longitude": origin[0], "latitude": origin[1]},
                    {"longitude": destination[0], "latitude": destination[1]},
                )
                road_km = straight * DETOUR_FACTOR
                total_km += road_km
                total_min += estimate_duration_min(mode, road_km)

        return round(total_km, 2), int(round(total_min))

    # ------------------------------------------------------------------ #
    # 天气
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_weather_info(
        dates: List[str],
        forecast: List[Dict[str, Any]],
    ) -> List[WeatherInfo]:
        """把高德预报对齐到行程日期(高德最多预报 4 天, 超出部分标记为暂无)。"""
        by_date = {item.get("date"): item for item in forecast}
        result: List[WeatherInfo] = []
        for date in dates:
            item = by_date.get(date)
            if not item:
                result.append(
                    WeatherInfo(
                        date=date,
                        day_weather="暂无预报",
                        night_weather="暂无预报",
                        day_temp=0,
                        night_temp=0,
                        wind_direction="",
                        wind_power="",
                    )
                )
                continue
            result.append(
                WeatherInfo(
                    date=date,
                    day_weather=item.get("day_weather", ""),
                    night_weather=item.get("night_weather", ""),
                    day_temp=safe_int(item.get("day_temp")),
                    night_temp=safe_int(item.get("night_temp")),
                    wind_direction=item.get("wind_direction", ""),
                    wind_power=item.get("wind_power", ""),
                )
            )
        return result


# ---------------------------------------------------------------------- #
# 单例
# ---------------------------------------------------------------------- #
_multi_agent_planner: Optional[MultiAgentTripPlanner] = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)。"""
    global _multi_agent_planner
    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()
    return _multi_agent_planner


def reset_trip_planner_agent() -> None:
    """重置单例(测试用)。"""
    global _multi_agent_planner
    _multi_agent_planner = None
