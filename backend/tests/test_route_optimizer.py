"""路线优化算法测试。"""

from app.planning.geo import haversine_km
from app.planning.route import nearest_neighbor_order, optimize_route, two_opt


def _point(lng: float, lat: float, name: str = "") -> dict:
    return {"name": name, "location": {"longitude": lng, "latitude": lat}}


def test_haversine_zero_distance():
    a = _point(116.397, 39.918)
    assert haversine_km(a["location"], a["location"]) == 0


def test_haversine_known_distance():
    """天安门 -> 故宫, 直线距离约 1 公里量级。"""
    a = _point(116.397, 39.909)
    b = _point(116.397, 39.918)
    assert 0.8 < haversine_km(a["location"], b["location"]) < 1.2


def test_nearest_neighbor_orders_by_proximity():
    points = [
        _point(0, 0, "A")["location"],
        _point(10, 0, "B")["location"],
        _point(0.5, 0, "C")["location"],
    ]
    order = nearest_neighbor_order(points)
    assert order[0] == 0
    assert order.index(2) < order.index(1)


def test_two_opt_shortens_path():
    points = [
        _point(0, 0, "A")["location"],
        _point(10, 10, "B")["location"],
        _point(0, 10, "C")["location"],
        _point(10, 0, "D")["location"],
    ]
    order = [0, 1, 2, 3]
    optimized = two_opt(points, order, fix_first=False)
    assert optimized != order  # 交叉线路被消除


def test_optimize_route_is_deterministic():
    points = [_point(i * 0.01, i * 0.01, f"P{i}") for i in range(6)]
    first = optimize_route(points)
    second = optimize_route(points)
    assert first["order"] == second["order"]
    assert first["total_km"] == second["total_km"]


def test_optimize_route_keeps_hotel_as_start():
    points = [
        _point(116.40, 39.92, "A"),
        _point(116.41, 39.93, "B"),
        _point(116.42, 39.94, "C"),
    ]
    start = {"longitude": 116.39, "latitude": 39.91}
    result = optimize_route(points, start)
    assert len(result["order"]) == len(points)
    assert result["total_km"] > 0
