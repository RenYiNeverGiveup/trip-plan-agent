"""预算计算。

预算完全由算法汇总真实数据得到(门票来自高德 POI 详情、餐饮与住宿来自模型建议并落在真实 POI 上、
交通由路线距离与交通方式模型推算), 而不是让模型直接给出一个总数, 因此各项之和必然等于总额。
"""

from typing import List, Optional

from ..models.schemas import Budget, DayPlan


def count_overnight_nights(days: List[DayPlan], include_last_night: bool = False) -> int:
    """住宿夜数: 默认按"最后一天离开"计算, 即天数 - 1。"""
    if not days:
        return 0
    if include_last_night:
        return len(days)
    return max(len(days) - 1, 0)


def compute_budget(
    days: List[DayPlan],
    include_last_night: bool = False,
) -> Budget:
    """根据每日行程汇总预算。"""
    total_attractions = 0
    total_meals = 0
    total_transportation = 0
    total_hotels = 0

    nights = count_overnight_nights(days, include_last_night)

    for index, day in enumerate(days):
        total_attractions += sum(a.ticket_price for a in day.attractions)
        total_meals += sum(m.estimated_cost for m in day.meals)
        total_transportation += getattr(day, "transport_cost", 0) or 0
        if index < nights and day.hotel is not None:
            total_hotels += day.hotel.estimated_cost or 0

    return Budget(
        total_attractions=total_attractions,
        total_hotels=total_hotels,
        total_meals=total_meals,
        total_transportation=total_transportation,
        total=total_attractions + total_hotels + total_meals + total_transportation,
    )
