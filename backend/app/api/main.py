"""FastAPI 主应用"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings, print_config, validate_config
from .routes import map as map_routes, poi, trip

logger = logging.getLogger(__name__)

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "智能旅行规划助手 API。基于自研 Agent 框架(function calling + 并行编排), "
        "行程中的景点、坐标、门票、距离与预算均来自高德地图真实数据。"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")


@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    logger.info("=" * 60)
    logger.info("🚀 %s v%s", settings.app_name, settings.app_version)
    logger.info("=" * 60)

    print_config()

    errors = validate_config()
    if errors:
        logger.error("配置校验失败: %s", "; ".join(errors))
        raise RuntimeError(
            "配置错误: " + "; ".join(errors) + " (请复制 .env.example 为 .env 后填写)"
        )

    logger.info("✅ 配置验证通过")
    logger.info("📚 API 文档: http://localhost:%s/docs", settings.port)


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    logger.info("👋 应用正在关闭...")


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "framework": "self-built agent framework",
        "docs": "/docs",
        "redoc": "/redoc",
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )
