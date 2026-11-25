"""地理计算工具。"""

import math
from typing import Dict, Optional

EARTH_RADIUS_KM = 6371.0088


def haversine_km(coord_a: Dict[str, float], coord_b: Dict[str, float]) -> float:
    """计算两点间大圆距离(公里)。"""
    lon1, lat1 = coord_a["longitude"], coord_a["latitude"]
    lon2, lat2 = coord_b["longitude"], coord_b["latitude"]
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def path_length_km(points: list) -> float:
    """路径总长度(公里)。"""
    total = 0.0
    for i in range(len(points) - 1):
        total += haversine_km(points[i], points[i + 1])
    return total


def centroid(points: list) -> Optional[Dict[str, float]]:
    """一组坐标的重心。"""
    valid = [p for p in points if p and p.get("longitude") is not None]
    if not valid:
        return None
    return {
        "longitude": sum(p["longitude"] for p in valid) / len(valid),
        "latitude": sum(p["latitude"] for p in valid) / len(valid),
    }
