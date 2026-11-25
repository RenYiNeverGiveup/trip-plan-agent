"""景点游览顺序优化。

思路: 最近邻构造初始解 + 2-opt 局部优化, 目标是最小化当日路网总长度。
这是纯确定性算法, 相比让 LLM "凭感觉"排序更可靠, 也让"游览顺序"可复现、可解释。
"""

from typing import Any, Dict, List, Optional, Sequence

from .geo import haversine_km, path_length_km


def nearest_neighbor_order(points: Sequence[Dict[str, Any]], start_index: int = 0) -> List[int]:
    """最近邻算法: 从 start_index 出发, 返回按顺序排列的下标列表。"""
    if not points:
        return []
    remaining = [i for i in range(len(points)) if i != start_index]
    order = [start_index]
    while remaining:
        last = points[order[-1]]
        nearest = min(remaining, key=lambda idx: haversine_km(last, points[idx]))
        order.append(nearest)
        remaining.remove(nearest)
    return order


def two_opt(points: Sequence[Dict[str, Any]], order: List[int], fix_first: bool = True) -> List[int]:
    """2-opt 局部优化: 反复尝试反转子路径, 只要能缩短总长度就接受。

    Args:
        fix_first: True 时不允许移动第一个点(通常是酒店起点)
    """
    if len(order) < 4:
        return list(order)

    best = list(order)
    best_length = path_length_km([points[i] for i in best])
    improved = True
    while improved:
        improved = False
        begin = 1 if fix_first else 0
        for i in range(begin, len(best) - 1):
            for j in range(i + 2, len(best)):
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                length = path_length_km([points[idx] for idx in candidate])
                if length + 1e-9 < best_length:
                    best, best_length = candidate, length
                    improved = True
    return best


def optimize_route(
    points: Sequence[Dict[str, Any]],
    start: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """优化一天的游览顺序。

    Args:
        points: 带 location 字段的 POI 列表(景点)
        start: 可选起点(如酒店坐标); 若提供, 会把起点固定在路径首位

    Returns:
        {"order": 景点下标列表, "total_km": 总距离, "legs": 每段距离(公里)}
    """
    if not points:
        return {"order": [], "total_km": 0.0, "legs": []}

    coordinates = [p["location"] for p in points]
    if start:
        augmented = [start] + coordinates
        order = nearest_neighbor_order(augmented, start_index=0)
        order = two_opt(augmented, order, fix_first=True)
        # 去掉起点(酒店), 并把下标还原到原始 points 的坐标系
        order = [idx - 1 for idx in order[1:]]
        path = [augmented[0]] + [coordinates[idx] for idx in order]
    else:
        order = nearest_neighbor_order(coordinates, start_index=0)
        order = two_opt(coordinates, order, fix_first=False)
        path = [coordinates[idx] for idx in order]

    legs = [
        round(haversine_km(path[i], path[i + 1]), 3)
        for i in range(len(path) - 1)
    ]
    total_km = round(sum(legs), 3)

    return {"order": order, "total_km": total_km, "legs": legs}
