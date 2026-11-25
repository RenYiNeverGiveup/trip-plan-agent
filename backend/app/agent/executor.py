"""Agent 执行器: 自研的 Thought-Action-Observation 循环。

核心流程:
    LLM 输出 tool_calls -> 执行工具 -> 把结果作为 tool 消息回传 -> 再次调用 LLM
    直到模型不再请求工具(输出最终答案), 或达到最大步数。

与"用提示词硬凑 [TOOL_CALL:...] 文本格式"的写法相比, 这里使用标准的
function calling, 参数由模型以结构化形式给出, 不需要再写脆弱的文本解析。
"""

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from .llm import LLMClient, LLMError
from .registry import ToolRegistry
from .types import ChatMessage, ToolCall
from .utils import extract_json

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Agent:
    """一个可调用工具的智能体。"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        llm: LLMClient,
        tools: Optional[ToolRegistry] = None,
        max_steps: int = 6,
        temperature: float = 0.2,
        verbose: bool = True,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.max_steps = max_steps
        self.temperature = temperature
        self.verbose = verbose
        #: 最近一次运行的可观测轨迹, 便于排查问题
        self.trace: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        if self.verbose:
            logger.info("[%s] %s", self.name, message)

    async def run(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """执行一轮完整推理, 返回最终文本答案。"""
        messages: List[ChatMessage] = [ChatMessage(role="system", content=self.system_prompt)]
        messages.append(ChatMessage(role="user", content=self._build_user_message(query, context)))
        self.trace = []

        for step in range(self.max_steps):
            response = await self.llm.chat(
                messages,
                tools=self.tools.to_openai_tools() or None,
                temperature=self.temperature,
            )
            messages.append(response)

            if not response.tool_calls:
                content = (response.content or "").strip()
                self.trace.append({"step": step, "type": "final", "content": content[:500]})
                self._log(f"第{step + 1}步: 输出最终答案")
                return content

            self._log(f"第{step + 1}步: 请求 {len(response.tool_calls)} 个工具 {[c.name for c in response.tool_calls]}")
            tool_messages = await self._execute_tools(response.tool_calls)
            messages.extend(tool_messages)

        self._log(f"达到最大步数 {self.max_steps}, 使用最后一次结果")
        last = messages[-1]
        return last.content or ""

    async def run_structured(
        self,
        query: str,
        model_cls: Type[T],
        context: Optional[Dict[str, Any]] = None,
    ) -> T:
        """执行推理并要求最终输出可被 `model_cls` 校验的结构化数据。"""
        messages: List[ChatMessage] = [ChatMessage(role="system", content=self.system_prompt)]
        messages.append(ChatMessage(role="user", content=self._build_user_message(query, context)))

        # 没有工具时直接走结构化输出通道(json_schema -> json_object -> prompt 自动降级)
        if not self.tools.list_tools():
            return await self.llm.structured(messages, model_cls, temperature=self.temperature)

        for step in range(self.max_steps):
            response = await self.llm.chat(
                messages,
                tools=self.tools.to_openai_tools() or None,
                temperature=self.temperature,
            )
            messages.append(response)

            if not response.tool_calls:
                content = (response.content or "").strip()
                if not content:
                    raise LLMError(f"{self.name} 返回了空内容")
                return await self.llm.validate_or_repair(messages, extract_json(content), model_cls)

            self._log(f"第{step + 1}步: 请求 {[c.name for c in response.tool_calls]}")
            messages.extend(await self._execute_tools(response.tool_calls))

        raise LLMError(f"{self.name} 在 {self.max_steps} 步内未产出结构化结果")

    # ------------------------------------------------------------------ #
    async def _execute_tools(self, tool_calls: List[ToolCall]) -> List[ChatMessage]:
        """并发执行工具调用, 单个工具失败不影响其它工具。"""
        results = await asyncio.gather(
            *(self._call_tool(call) for call in tool_calls),
            return_exceptions=False,
        )
        messages: List[ChatMessage] = []
        for call, result in zip(tool_calls, results):
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=result,
                )
            )
        return messages

    async def _call_tool(self, call: ToolCall) -> str:
        started = time.perf_counter()
        spec = self.tools.get(call.name)
        if spec is None:
            content = f"错误: 不存在名为 {call.name} 的工具, 可用工具: {self.tools.list_tools()}"
        else:
            try:
                value = spec.handler(**call.arguments)
                if inspect.isawaitable(value):
                    value = await value
                content = json.dumps(value, ensure_ascii=False, default=str)
            except TypeError as exc:
                content = f"错误: 工具 {call.name} 参数不合法: {exc}"
            except Exception as exc:  # noqa: BLE001 - 工具异常需回传给模型自行纠正
                logger.warning("工具 %s 执行失败: %s", call.name, exc)
                content = f"错误: 工具 {call.name} 执行失败: {exc}"

        elapsed = time.perf_counter() - started
        self.trace.append(
            {
                "step": len(self.trace),
                "type": "tool",
                "tool": call.name,
                "arguments": call.arguments,
                "result": content[:500],
                "elapsed_ms": round(elapsed * 1000, 1),
            }
        )
        self._log(f"  -> {call.name}({elapsed * 1000:.0f}ms) 返回 {len(content)} 字符")
        return content

    @staticmethod
    def _build_user_message(query: str, context: Optional[Dict[str, Any]]) -> str:
        if not context:
            return query
        context_text = json.dumps(context, ensure_ascii=False, default=str)
        return f"{query}\n\n【上下文数据(JSON)】\n{context_text}"
