"""自研 Agent 框架的基础能力测试(工具 schema / JSON 解析 / 并行编排)。"""

import asyncio

import pytest

from app.agent.orchestrator import run_all_successful, run_parallel
from app.agent.registry import ToolRegistry, build_tool
from app.agent.utils import extract_json


# ---------------------------------------------------------------------- #
# 工具 schema 推断
# ---------------------------------------------------------------------- #
def test_build_tool_infers_schema():
    def search_poi(keywords: str, city: str, limit: int = 10, ratio: float = 0.5, tags: list = None):
        """搜索兴趣点"""
        return []

    spec = build_tool("search_poi", "搜索兴趣点", search_poi)
    params = spec.parameters
    assert params["type"] == "object"
    assert params["properties"]["keywords"]["type"] == "string"
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["ratio"]["type"] == "number"
    assert set(params["required"]) == {"keywords", "city"}
    assert spec.description == "搜索兴趣点"


def test_build_tool_optional_annotation():
    def f(a: str, b: int = 1, c: "bool | None" = None):
        """测试"""
        return None

    spec = build_tool("f", "测试", f)
    assert "b" not in spec.parameters.get("required", [])
    assert "c" not in spec.parameters.get("required", [])


def test_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(build_tool("a", "A", lambda: None))
    with pytest.raises(ValueError):
        registry.register(build_tool("a", "A", lambda: None))


def test_registry_to_openai_tools():
    registry = ToolRegistry()
    registry.register(build_tool("a", "A", lambda x: x))
    tools = registry.to_openai_tools()
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "a"
    assert registry.list_tools() == ["a"]


# ---------------------------------------------------------------------- #
# JSON 解析
# ---------------------------------------------------------------------- #
def test_extract_json_plain():
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_code_fence():
    text = '这是结果:\n```json\n{"a": 1}\n```\n以上'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_with_prose():
    text = '好的, 计划如下 {"a": 1} 请查收'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        extract_json("完全不是 JSON")


# ---------------------------------------------------------------------- #
# 并行编排
# ---------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_parallel_returns_all_results():
    async def a():
        await asyncio.sleep(0.01)
        return "A"

    async def b():
        return "B"

    results = await run_parallel({"a": a, "b": b})
    assert results == {"a": "A", "b": "B"}


@pytest.mark.asyncio
async def test_run_parallel_isolates_failures():
    async def ok():
        return 1

    async def boom():
        raise RuntimeError("炸了")

    results = await run_parallel({"ok": ok, "boom": boom})
    assert results["ok"] == 1
    assert isinstance(results["boom"], RuntimeError)

    filtered = await run_all_successful(results)
    assert list(filtered.keys()) == ["ok"]


@pytest.mark.asyncio
async def test_run_all_successful_raises_when_everything_fails():
    async def boom():
        raise RuntimeError("炸了")

    results = await run_parallel({"boom": boom})
    with pytest.raises(Exception):
        await run_all_successful(results)
