"""MCP 适配层测试(不需要真实 MCP server)。"""

import pytest

from app.agent.registry import ToolRegistry
from app.agent.toolkits.amap_tools import build_amap_tools
from app.agent.toolkits.mcp_amap import (
    CANONICAL_NAMES,
    McpAmapProvider,
    classify_mcp_tool,
)


class _FakeMcpTool:
    def __init__(self, name: str, description: str = "", schema: dict = None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeSession:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        class Result:
            pass

        result = Result()
        result.tools = self._tools
        return result

    async def call_tool(self, name, arguments):
        class Block:
            def __init__(self, text):
                self.text = text

        class Result:
            content = [Block('{"pois": [{"id": "B001", "name": "某某景点"}]}')]
            structuredContent = None

        return Result()


class _FakeProvider(McpAmapProvider):
    def __init__(self, tools):
        super().__init__(command=["fake"])
        self._session = _FakeSession(tools)


def test_classify_mcp_tool():
    assert classify_mcp_tool("amap_maps_text_search") == "search"
    assert classify_mcp_tool("amap_maps_weather") == "weather"
    assert classify_mcp_tool("amap_maps_around") == "around"
    assert classify_mcp_tool("amap_maps_detail") == "detail"
    assert classify_mcp_tool("amap_maps_distance") == "route"
    assert classify_mcp_tool("some_unknown_tool") is None


def test_canonical_names_cover_roles():
    assert set(CANONICAL_NAMES.values()) == {
        "amap_search_poi",
        "amap_search_around",
        "amap_weather",
        "amap_poi_detail",
        "amap_route_distance",
    }


@pytest.mark.asyncio
async def test_mcp_tools_are_renamed_to_canonical_names():
    provider = _FakeProvider(
        [
            _FakeMcpTool("amap_maps_text_search", "关键字搜索"),
            _FakeMcpTool("amap_maps_weather", "天气"),
        ]
    )
    registry = await build_amap_tools(client=None, mcp_provider=provider)
    names = registry.list_tools()
    assert "amap_search_poi" in names
    assert "amap_weather" in names


@pytest.mark.asyncio
async def test_mcp_call_result_is_parsed_as_json():
    provider = _FakeProvider([_FakeMcpTool("amap_maps_text_search", "关键字搜索")])
    registry = await build_amap_tools(client=None, mcp_provider=provider)
    result = await registry.get("amap_search_poi").handler(keywords="故宫", city="北京")
    assert result["pois"][0]["id"] == "B001"


@pytest.mark.asyncio
async def test_fallback_to_rest_when_mcp_not_connected():
    """MCP 未连接时必须回落到 REST 直连工具集。"""
    from app.services.amap_service import AmapClient

    client = AmapClient(api_key="dummy")
    registry = await build_amap_tools(client=client, mcp_provider=None)
    names = registry.list_tools()
    assert "amap_search_poi" in names
    assert "amap_weather" in names
    assert "amap_search_around" in names
    assert "amap_route_distance" in names


@pytest.mark.asyncio
async def test_provider_start_returns_false_without_sdk(monkeypatch):
    """没有装 mcp SDK 时 start() 返回 False, 而不是抛异常。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("mcp"):
            raise ImportError("no mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    provider = McpAmapProvider(command=["fake"])
    assert await provider.start() is False
