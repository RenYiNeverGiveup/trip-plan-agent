"""配置管理模块。

所有配置来自环境变量或 `.env` 文件, 不依赖任何 Agent 框架的隐式约定。
"""

import logging
from typing import List

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

# 优先加载 backend/.env
load_dotenv()

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """应用配置。"""

    # 应用基本配置
    app_name: str = "Trip Plan Agent 智能旅行助手"
    app_version: str = "2.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置(逗号分隔)
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # ---------- LLM ----------
    # 使用 OpenAI 兼容协议, 因此 DeepSeek / 通义 / Kimi 等均可直接接入
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    # 环境变量优先 LLM_MODEL_ID, 兼容 LLM_MODEL
    llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("LLM_MODEL_ID", "LLM_MODEL"),
    )
    llm_temperature: float = 0.3
    llm_timeout: float = 90.0
    llm_max_retries: int = 2
    # 结构化输出模式: auto / json_schema / json_object / prompt
    llm_structured_mode: str = "auto"
    # 单个 Agent 允许的最大推理步数
    llm_max_steps: int = 6

    # ---------- 高德地图 ----------
    amap_api_key: str = ""
    amap_base_url: str = "https://restapi.amap.com/v3"
    amap_timeout: float = 15.0
    # 每天安排的景点数量
    attractions_per_day: int = 3

    # ---------- MCP(可选) ----------
    # 开启后通过官方 MCP SDK 连接 amap-mcp-server, 连接失败自动回落到 REST 直连
    use_mcp_amap: bool = False
    mcp_amap_command: str = "uvx amap-mcp-server"
    mcp_amap_timeout: float = 30.0

    # ---------- Unsplash ----------
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # 日志级别
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

    def get_cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()


def get_settings() -> Settings:
    """获取配置单例。"""
    return settings


def validate_config() -> List[str]:
    """校验必要配置, 返回错误列表(空列表表示通过)。"""
    errors: List[str] = []
    warnings: List[str] = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY 未配置")
    if not settings.llm_api_key:
        errors.append("LLM_API_KEY 未配置")
    if not settings.llm_base_url:
        warnings.append("LLM_BASE_URL 未配置, 将使用 OpenAI 官方地址")
    if not settings.unsplash_access_key:
        warnings.append("UNSPLASH_ACCESS_KEY 未配置, 景点配图将不可用")

    for error in errors:
        logger.error("配置错误: %s", error)
    for warning in warnings:
        logger.warning("配置警告: %s", warning)
    return errors


def print_config() -> None:
    """打印配置信息(隐藏敏感项)。"""
    print(f"应用名称  : {settings.app_name} v{settings.app_version}")
    print(f"LLM       : {settings.llm_model} @ {settings.llm_base_url} (key: {'已配置' if settings.llm_api_key else '未配置'})")
    print(f"高德地图  : key {'已配置' if settings.amap_api_key else '未配置'}")
    print(f"Unsplash  : key {'已配置' if settings.unsplash_access_key else '未配置'}")
    print(f"日志级别  : {settings.log_level}")
