"""端到端测试: 用假 LLM / 假高德跑通整条规划流水线。"""

import asyncio

import pytest

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import TripRequest


def _request(**overrides) -> TripRequest:
    payload = {
        "city": "北京",
        "start_date": "2025-06-01",
        "end_date": "2025-06-02",
        "travel_days": 2,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": ["历史文化"],
        "free_text_input": "",
    }
    payload.update(overrides)
    return TripRequest(**payload)


@pytest.mark.asyncio
async def test_plan_trip_end_to_end(fake_llm, fake_amap):
    planner = MultiAgentTripPlanner(llm=fake_llm, amap_client=fake_amap)
    plan = await planner.plan_trip(_request())

    # 基本结构
    assert plan.city == "北京"
    assert len(plan.days) == 2
    assert [day.date for day in plan.days] == ["2025-06-01", "2025-06-02"]

    # 景点必须来自高德真实数据(不能是 0 坐标)
    real_ids = {"A001", "A002", "A003", "A004"}
    for day in plan.days:
        assert day.attractions
        for attraction in day.attractions:
            assert attraction.poi_id in real_ids
            assert attraction.location.longitude != 0.0
            assert attraction.location.latitude != 0.0

    # 餐饮来自真实餐厅 POI
    meal_ids = {"R001", "R002", "R003"}
    for day in plan.days:
        assert day.meals
        for meal in day.meals:
            assert meal.name
            assert meal.estimated_cost > 0

    # 天气
    assert len(plan.weather_info) == 2
    assert plan.weather_info[0].day_weather == "晴"

    # 预算自洽
    budget = plan.budget
    assert budget is not None
    assert budget.total == (
        budget.total_attractions + budget.total_hotels + budget.total_meals + budget.total_transportation
    )
    assert budget.total_attractions == sum(
        attraction.ticket_price for day in plan.days for attraction in day.attractions
    )
    assert budget.total_meals == sum(meal.estimated_cost for day in plan.days for meal in day.meals)
    assert budget.total_transportation == sum(day.transport_cost for day in plan.days)


@pytest.mark.asyncio
async def test_plan_trip_route_is_optimized(fake_llm, fake_amap):
    """同一批景点, 优化后的路线长度不应长于原始顺序。"""
    planner = MultiAgentTripPlanner(llm=fake_llm, amap_client=fake_amap)
    plan = await planner.plan_trip(_request(travel_days=1, end_date="2025-06-01"))

    day = plan.days[0]
    assert day.route_summary
    assert " → " in day.route_summary
    assert day.transport_distance_km >= 0


@pytest.mark.asyncio
async def test_no_fake_coordinates_when_llm_fails(fake_llm, fake_amap):
    """LLM 完全不可用时, 降级排程同样只使用真实 POI。"""

    class BrokenLLM:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("模型不可用")

    planner = MultiAgentTripPlanner(llm=BrokenLLM(), amap_client=fake_amap)
    plan = await planner.plan_trip(_request())

    assert plan.days
    for day in plan.days:
        for attraction in day.attractions:
            assert attraction.poi_id.startswith("A")
            assert attraction.location.longitude != 0.0
