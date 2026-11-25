"""旅行规划API路由"""

import logging

from fastapi import APIRouter, HTTPException

from ...agents.trip_planner_agent import TripPlanningError, get_trip_planner_agent
from ...models.schemas import TripPlanResponse, TripRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划"
)
async def plan_trip(request: TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应
    """
    try:
        logger.info(
            "收到旅行规划请求: %s %s-%s (%s天)",
            request.city,
            request.start_date,
            request.end_date,
            request.travel_days,
        )

        agent = get_trip_planner_agent()
        trip_plan = await agent.plan_trip(request)

        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan
        )

    except TripPlanningError as e:
        logger.error("旅行计划生成失败: %s", e)
        raise HTTPException(status_code=502, detail=f"旅行计划生成失败: {str(e)}")
    except Exception as e:
        logger.exception("旅行计划生成异常")
        raise HTTPException(status_code=500, detail=f"旅行计划生成失败: {str(e)}")


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        agent = get_trip_planner_agent()
        await agent.ensure_tools()

        return {
            "status": "healthy",
            "service": "trip-planner",
            "llm_model": agent.llm.model,
            "tools_count": len(agent.tools.list_tools()),
            "tool_names": agent.tools.list_tools(),
            "framework": "self-built",
            "tool_backend": "mcp" if agent.mcp_provider else "rest",
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"服务不可用: {str(e)}")
