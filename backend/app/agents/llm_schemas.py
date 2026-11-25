"""LLM 结构化输出契约。

这些模型是"模型与确定性代码之间的接口":
- 采集 Agent 返回候选 POI(但坐标/地址会被 POIStore 用高德原始数据覆盖)
- 规划 Agent 只返回"选择哪些 id、怎么描述", 不允许自己编造事实
"""

from typing import List, Optional

from pydantic import BaseModel, Field

from ..models.schemas import Location


class CandidatePOI(BaseModel):
    """候选 POI(来自工具返回的真实数据)。"""

    id: str = Field(..., description="POI 唯一 ID, 必须来自工具返回结果")
    name: str = Field(..., description="POI 名称")
    address: str = Field(default="", description="地址")
    location: Location = Field(default_factory=lambda: Location(longitude=0.0, latitude=0.0))
    rating: Optional[float] = Field(default=None, description="评分")
    ticket_price: int = Field(default=0, description="门票价格(元)")
    category: str = Field(default="", description="类别, 例如 博物馆/公园/古镇")
    description: str = Field(default="", description="一句话介绍")
    visit_duration: int = Field(default=120, description="建议游览时长(分钟)")
    price_range: str = Field(default="", description="价格区间, 酒店用")
    estimated_cost: int = Field(default=0, description="预估价格(元), 酒店/餐厅用")


class CandidateList(BaseModel):
    """候选集合。"""

    pois: List[CandidatePOI] = Field(default_factory=list)


class DaySelection(BaseModel):
    """某一天的景点与住宿选择。"""

    day_index: int = Field(..., description="第几天, 从 0 开始")
    description: str = Field(default="", description="当日行程主题描述, 一到两句话")
    attraction_ids: List[str] = Field(default_factory=list, description="当日景点 id 列表, 建议 2-4 个")
    hotel_id: Optional[str] = Field(default=None, description="当晚住宿的酒店 id")
    notes: str = Field(default="", description="补充提醒, 例如需要预约")


class PlanSelection(BaseModel):
    """整趟行程的选择结果。"""

    days: List[DaySelection] = Field(default_factory=list)
    overall_suggestions: str = Field(default="", description="整体出行建议, 3-5 条要点")


class MealSelection(BaseModel):
    """某一餐的选择。"""

    meal_type: str = Field(..., description="餐次: breakfast/lunch/dinner/snack")
    restaurant_id: str = Field(..., description="餐厅 id, 必须来自候选列表")
    estimated_cost: int = Field(default=0, description="人均预估消费(元)")
    description: str = Field(default="", description="推荐理由")


class DayMealPlan(BaseModel):
    """某一天的餐饮安排。"""

    day_index: int = Field(..., description="第几天, 从 0 开始")
    meals: List[MealSelection] = Field(default_factory=list)


class MealPlan(BaseModel):
    """所有天的餐饮安排。"""

    days: List[DayMealPlan] = Field(default_factory=list)
