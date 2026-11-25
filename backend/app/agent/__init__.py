"""自研 Agent 框架。

模块划分:
- types.py       : 消息 / 工具调用 / 工具声明
- registry.py    : 工具注册表与 JSON Schema 自动推断
- llm.py         : OpenAI 兼容客户端(对话 + 结构化输出)
- executor.py    : Thought-Action-Observation 循环
- orchestrator.py: 多任务并行编排
- toolkits/      : 具体工具集(高德地图等)
- utils.py       : 通用工具函数
"""

from .executor import Agent
from .llm import LLMClient, LLMError
from .orchestrator import OrchestrationError, run_parallel, run_all_successful
from .registry import ToolRegistry, build_tool
from .types import ChatMessage, ToolCall, ToolSpec
from .utils import extract_json, json_dumps

__all__ = [
    "Agent",
    "ChatMessage",
    "LLMClient",
    "LLMError",
    "OrchestrationError",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "build_tool",
    "extract_json",
    "json_dumps",
    "run_all_successful",
    "run_parallel",
]
