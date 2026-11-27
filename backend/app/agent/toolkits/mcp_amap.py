"""基于官方 MCP Python SDK 的高德地图工具提供者。

与 REST 直连并存, 通过环境变量 `USE_MCP_AMAP=true` 开启:

- 开启后: 通过 stdio 启动 `amap-mcp-server`, 工具清单由 `list_tools()` 动态生成,
  工具调用走 `session.call_tool()`; MCP server 起不来时**自动回落到 REST 直连**。
- 关闭时(默认): 只用 REST 直连, 无需安装 `uv`/`uvx`, 无需额外进程。

工具名会被统一映射成本项目的规范名(amap_search_poi / amap_weather / ...),
这样无论走哪条链路, 提示词与 Agent 的行为都保持一致。
"""

import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from ...logging_config import Timer, dumps
from ..registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

# MCP server 工具名 -> 本项目规范名
ROLE_BY_KEYWORD = (
    ("around", "around"),
    ("weather", "weather"),
    ("detail", "detail"),
    ("text_search", "search"),
    ("search", "search"),
    ("distance", "route"),
    ("direction", "route"),
    ("walk", "route"),
    ("drive", "route"),
    ("transit", "route"),
)

CANONICAL_NAMES = {
    "search": "amap_search_poi",
    "around": "amap_search_around",
    "weather": "amap_weather",
    "detail": "amap_poi_detail",
    "route": "amap_route_distance",
}


def classify_mcp_tool(name: str) -> Optional[str]:
    """根据工具名推断其能力角色。"""
    lowered = name.lower()
    for keyword, role in ROLE_BY_KEYWORD:
        if keyword in lowered:
            return role
    return None


class McpAmapProvider:
    """官方 MCP SDK 客户端封装。"""

    def __init__(
        self,
        command: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> None:
        self.command = command or ["uvx", "amap-mcp-server"]
        self.env = env or {}
        self.timeout = timeout
        self._stack: Optional[AsyncExitStack] = None
        self._session: Any = None
        self._original_names: Dict[str, str] = {}  # 规范名 -> MCP 原始工具名

    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._session is not None

    async def start(self) -> bool:
        """连接 MCP server; 失败返回 False(调用方回落到 REST)。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.warning("未安装 mcp SDK, 跳过 MCP 模式(使用 REST 直连)")
            return False

        try:
            self._stack = AsyncExitStack()
            params = StdioServerParameters(
                command=self.command,
                env={**os.environ, **self.env},
            )
            transport = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(transport))
            await self._session.initialize()
            logger.info("MCP 高德服务已连接: %s", " ".join(self.command))
            return True
        except Exception as exc:  # noqa: BLE001 - 任何启动失败都回落到 REST
            logger.warning("MCP 连接失败(%s), 回落到 REST 直连: %s", " ".join(self.command), exc)
            await self.close()
            return False

    async def close(self) -> None:
        """关闭连接并释放子进程。"""
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 MCP 连接时出错: %s", exc)
        self._stack = None
        self._session = None
        self._original_names.clear()

    # ------------------------------------------------------------------ #
    async def list_specs(self) -> List[ToolSpec]:
        """把 MCP 工具清单转换为本项目的 ToolSpec。"""
        if self._session is None:
            return []

        result = await self._session.list_tools()
        specs: List[ToolSpec] = []
        used: set = set()

        for tool in result.tools:
            role = classify_mcp_tool(tool.name)
            canonical = CANONICAL_NAMES.get(role or "", tool.name)
            if canonical in used:  # 名称冲突时保留原名
                canonical = tool.name
            used.add(canonical)

            schema = tool.inputSchema or {"type": "object", "properties": {}}
            original = tool.name
            self._original_names[canonical] = original
            specs.append(
                ToolSpec(
                    name=canonical,
                    description=tool.description or tool.name,
                    parameters=schema,
                    handler=self._make_handler(original),
                )
            )
        return specs

    def _make_handler(self, original_name: str):
        async def handler(**arguments: Any) -> Any:
            return await self.call(original_name, arguments)

        handler.__name__ = original_name
        return handler

    async def call(self, name: str, arguments: Dict[str, Any]) -> Any:
        """调用 MCP 工具, 并把返回内容解析成 JSON 结构(便于 POI 溯源)。"""
        if self._session is None:
            raise RuntimeError("MCP 连接未建立")

        timer = Timer()
        logger.info("→ MCP 工具调用 %s args=%s", name, dumps(arguments, 500))
        result = await self._session.call_tool(name, arguments)

        structured = getattr(result, "structuredContent", None)
        if structured:
            logger.info(
                "← MCP 工具返回 %s elapsed=%.0fms data=%s",
                name,
                timer.elapsed_ms,
                dumps(structured, 800),
            )
            return structured

        texts: List[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        payload = "\n".join(texts).strip()

        logger.info(
            "← MCP 工具返回 %s elapsed=%.0fms data=%s",
            name,
            timer.elapsed_ms,
            dumps(payload, 800),
        )

        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"text": payload}


async def build_mcp_registry(provider: McpAmapProvider) -> ToolRegistry:
    """异步构建: 拉取 MCP 工具清单并注册。"""
    specs = await provider.list_specs()
    if not specs:
        logger.warning("MCP server 未返回任何工具")
    return ToolRegistry(specs)
