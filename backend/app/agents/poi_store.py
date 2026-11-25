"""POI 仓库: 记录工具真正返回过的 POI。

这是本项目"事实性保证"的关键机制:
Agent 采集到的每一个 POI 都会进入仓库, 后续无论哪个 Agent 提到某个 id 或名称,
我们都能回到**高德返回的原始数据**去取经纬度、地址、票价, 从而杜绝模型编造坐标。
"""

from typing import Any, Dict, List, Optional


class POIStore:
    """id -> POI 数据的注册表。"""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._by_name: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    def add(self, poi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """登记一个 POI; 已存在时用新数据覆盖(详情数据更完整)。"""
        if not poi:
            return None
        poi_id = str(poi.get("id") or "").strip()
        name = str(poi.get("name") or "").strip()
        if not poi_id and not name:
            return None

        existing = self._items.get(poi_id) if poi_id else None
        if existing:
            merged = {**existing, **{k: v for k, v in poi.items() if v not in (None, "", [], {})}}
            self._items[poi_id] = merged
            item = merged
        else:
            item = dict(poi)
            if poi_id:
                self._items[poi_id] = item

        if name:
            self._by_name.setdefault(name, item)
        return item

    def add_many(self, pois: List[Dict[str, Any]]) -> None:
        for poi in pois or []:
            self.add(poi)

    def get(self, poi_id: str) -> Optional[Dict[str, Any]]:
        return self._items.get(str(poi_id).strip())

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        key = str(name).strip()
        if key in self._by_name:
            return self._by_name[key]
        for stored_name, item in self._by_name.items():
            if key in stored_name or stored_name in key:
                return item
        return None

    def values(self) -> List[Dict[str, Any]]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)
