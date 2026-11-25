"""交通方式模型: 把"用户选择的交通方式"映射到速度与费用。

费用模型参考国内主要城市公开价格量级, 属于"预算估算", 最终以实际为准。
"""

from dataclasses import dataclass
from typing import Dict, Tuple

WALKING = "walking"
DRIVING = "driving"
TRANSIT = "transit"
CYCLING = "cycling"


@dataclass(frozen=True)
class TransportMode:
    key: str
    label: str
    speed_kmh: float
    base_cost: float  # 起步价(元)
    included_km: float  # 起步价包含的里程
    per_km_cost: float  # 超出里程的单价(元/公里)
    step_km: float = 0.0  # 分段计价步长(公共交通)


MODES: Dict[str, TransportMode] = {
    WALKING: TransportMode(WALKING, "步行", 4.5, 0.0, 0.0, 0.0),
    CYCLING: TransportMode(CYCLING, "骑行", 15.0, 0.0, 0.0, 0.0),
    DRIVING: TransportMode(DRIVING, "自驾/打车", 25.0, 13.0, 3.0, 2.6),
    TRANSIT: TransportMode(TRANSIT, "公共交通", 20.0, 3.0, 6.0, 1.0, step_km=6.0),
}

# 用户输入 -> 内部模式
_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("步行", WALKING),
    ("走路", WALKING),
    ("徒步", WALKING),
    ("骑行", CYCLING),
    ("单车", CYCLING),
    ("自行车", CYCLING),
    ("电动车", CYCLING),
    ("自驾", DRIVING),
    ("开车", DRIVING),
    ("打车", DRIVING),
    ("驾车", DRIVING),
    ("出租", DRIVING),
    ("公交", TRANSIT),
    ("地铁", TRANSIT),
    ("公共交通", TRANSIT),
    ("地铁公交", TRANSIT),
)


def resolve_mode(text: str) -> str:
    """把用户填写的交通方式归一化为内部模式。"""
    if not text:
        return TRANSIT
    normalized = text.strip().lower()
    for alias, mode in _ALIASES:
        if alias in normalized:
            return mode
    return TRANSIT


def estimate_cost(mode: str, distance_km: float) -> int:
    """估算一段行程的交通费用(元)。"""
    spec = MODES.get(mode, MODES[TRANSIT])
    if distance_km <= 0:
        return 0
    if spec.key == TRANSIT:
        steps = max(0, int((distance_km - spec.included_km) // spec.step_km) + (1 if distance_km > spec.included_km else 0))
        return int(round(spec.base_cost + steps * spec.per_km_cost))
    if spec.key == DRIVING:
        extra = max(0.0, distance_km - spec.included_km)
        return int(round(spec.base_cost + extra * spec.per_km_cost))
    return 0


def estimate_duration_min(mode: str, distance_km: float) -> int:
    """估算一段行程的耗时(分钟), 向上取整。"""
    spec = MODES.get(mode, MODES[TRANSIT])
    if distance_km <= 0:
        return 0
    minutes = distance_km / spec.speed_kmh * 60
    return max(1, int(minutes + 0.999))


def mode_label(mode: str) -> str:
    return MODES.get(mode, MODES[TRANSIT]).label
