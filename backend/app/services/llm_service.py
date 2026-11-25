"""LLM 服务模块: 对自研 Agent 框架的 LLMClient 做单例封装。"""

import logging
from typing import Optional

from ..agent.llm import LLMClient
from ..config import get_settings

logger = logging.getLogger(__name__)

_llm_instance: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """获取 LLM 客户端(单例)。

    Returns:
        LLMClient 实例
    """
    global _llm_instance

    if _llm_instance is None:
        settings = get_settings()
        _llm_instance = LLMClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
            structured_mode=settings.llm_structured_mode,
        )
        logger.info("LLM 客户端初始化成功: %s @ %s", settings.llm_model, settings.llm_base_url)

    return _llm_instance


def reset_llm() -> None:
    """重置 LLM 客户端(测试或重新配置时使用)。"""
    global _llm_instance
    _llm_instance = None
