"""预算与交通模型测试。"""

from app.models.schemas import Attraction, DayPlan, Hotel, Location, Meal
from app.planning.budget import compute_budget, count_overnight_nights
from app.planning.transport import estimate_cost, estimate_duration_min, resolve_mode


def _day(index: int, ticket: int = 60, hotel_cost: int = 400, meal_cost: int = 50, transport: int = 20) -> DayPlan:
    return DayPlan(
        date=f"2025-06-0{index + 1}",
        day_index=index,
        description="测试行程",
        transportation="公共交通",
        accommodation="经济型酒店",
        hotel=Hotel(
            name="测试酒店",
            address="地址",
            location=Location(longitude=116.4, latitude=39.9),
            price_range="300-500元",
            rating="4.5",
            distance="",
            type="酒店",
            estimated_cost=hotel_cost,
        ),
        attractions=[
            Attraction(
                name="测试景点",
                address="地址",
                location=Location(longitude=116.4, latitude=39.9),
                visit_duration=120,
                description="",
                ticket_price=ticket,
            )
        ],
        meals=[
            Meal(type="lunch", name="测试餐厅", description="", estimated_cost=meal_cost),
        ],
        transport_cost=transport,
    )


def test_count_overnight_nights():
    days = [_day(0), _day(1), _day(2)]
    assert count_overnight_nights(days) == 2
    assert count_overnight_nights(days, include_last_night=True) == 3
    assert count_overnight_nights([]) == 0


def test_budget_excludes_last_night_hotel():
    days = [_day(0), _day(1)]
    budget = compute_budget(days)
    # 门票 60*2, 住宿 400*1(最后一晚不住), 餐饮 50*2, 交通 20*2
    assert budget.total_attractions == 120
    assert budget.total_hotels == 400
    assert budget.total_meals == 100
    assert budget.total_transportation == 40
    assert budget.total == 660


def test_budget_total_equals_sum_of_parts():
    days = [_day(0, ticket=100, meal_cost=80, transport=35), _day(1, ticket=20)]
    budget = compute_budget(days)
    assert budget.total == (
        budget.total_attractions
        + budget.total_hotels
        + budget.total_meals
        + budget.total_transportation
    )


def test_resolve_mode():
    assert resolve_mode("公共交通") == "transit"
    assert resolve_mode("地铁") == "transit"
    assert resolve_mode("自驾") == "driving"
    assert resolve_mode("打车") == "driving"
    assert resolve_mode("步行") == "walking"
    assert resolve_mode("骑行") == "cycling"
    assert resolve_mode("") == "transit"


def test_transport_cost_model():
    assert estimate_cost("walking", 3.0) == 0
    assert estimate_cost("driving", 2.0) == 13  # 起步价
    assert estimate_cost("driving", 10.0) == 13 + int(round(7 * 2.6))
    assert estimate_cost("transit", 5.0) == 3
    assert estimate_cost("transit", 15.0) == 3 + int((15 - 6) // 6) * 1 + 1


def test_transport_duration():
    assert estimate_duration_min("walking", 4.5) == 60
    assert estimate_duration_min("driving", 25.0) == 60
    assert estimate_duration_min("walking", 0) == 0
