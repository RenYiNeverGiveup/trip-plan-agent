"""LLM 客户端封装。

只依赖 OpenAI 兼容协议, 因此同一份代码可以对接:
- OpenAI / Azure OpenAI
- DeepSeek (https://api.deepseek.com/v1)
- 通义千问 (DashScope compatible-mode)
- Moonshot / Kimi / 智谱 等

三个核心能力:
1. `chat()`      : 普通对话 + function calling
2. `structured()`: 结构化输出(自动在 json_schema / json_object / prompt 三种模式间降级)
3. `usage`       : 累计 token 用量, 便于成本统计
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from .types import ChatMessage, ToolCall

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """LLM 调用失败。"""


class LLMClient:
    """OpenAI 兼容协议的异步 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "gpt-4o-mini",
        temperature: float = 0.3,
        timeout: float = 60.0,
        max_retries: int = 2,
        structured_mode: str = "auto",
    ) -> None:
        if not api_key:
            raise LLMError("未配置 LLM API Key (LLM_API_KEY)")

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少依赖 openai, 请执行 pip install openai") from exc

        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.structured_mode = structured_mode

        kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)

        self.usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # ------------------------------------------------------------------ #
    # 基础对话
    # ------------------------------------------------------------------ #
    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
    ) -> ChatMessage:
        """调用模型, 返回一条 assistant 消息。"""
        payload: List[Dict[str, Any]] = [m.to_dict() for m in messages]
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = tool_choice or "auto"
        if response_format:
            request["response_format"] = response_format

        response = await self._request(request)
        choice = response.choices[0].message

        if getattr(response, "usage", None):
            self._accumulate_usage(response.usage)

        tool_calls: List[ToolCall] = []
        for call in getattr(choice, "tool_calls", None) or []:
            arguments: Dict[str, Any] = {}
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                logger.warning("工具调用参数不是合法 JSON: %s", raw_args[:200])
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        return ChatMessage(role="assistant", content=choice.content, tool_calls=tool_calls)

    async def _request(self, request: Dict[str, Any]) -> Any:
        """带指数退避重试的请求。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._client.chat.completions.create(**request)
            except Exception as exc:  # noqa: BLE001 - 需要按 provider 异常类型区分
                last_error = exc
                if not self._is_retryable(exc) or attempt == self.max_retries:
                    break
                delay = min(2 ** attempt, 8)
                logger.warning("LLM 调用失败(%s/%s): %s, %.1fs 后重试", attempt + 1, self.max_retries + 1, exc, delay)
                await asyncio.sleep(delay)
        raise LLMError(f"LLM 调用失败: {last_error}") from last_error

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """区分"可重试的网络/限流错误"与"请求本身有问题"。"""
        try:
            from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
        except ImportError:  # pragma: no cover
            return False
        if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code >= 500 or exc.status_code == 429
        return False

    def _accumulate_usage(self, usage: Any) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value:
                self.usage[key] = self.usage.get(key, 0) + value

    # ------------------------------------------------------------------ #
    # 结构化输出
    # ------------------------------------------------------------------ #
    async def structured(
        self,
        messages: List[ChatMessage],
        model_cls: Type[T],
        temperature: float = 0.1,
    ) -> T:
        """要求模型返回能被 `model_cls` 校验的 JSON。

        会依次尝试 json_schema -> json_object -> prompt 三种模式,
        若校验失败则带着错误信息回传一次让模型自行修正。
        """
        schema = model_cls.model_json_schema()
        modes = self._structured_modes(schema)

        last_error: Optional[Exception] = None
        for mode in modes:
            request_messages = list(messages)
            if mode == "prompt":
                request_messages = self._inject_schema_prompt(request_messages, schema)

            try:
                raw = await self._chat_raw(
                    request_messages,
                    response_format=self._response_format(mode, schema, model_cls),
                    temperature=temperature,
                )
            except LLMError as exc:
                # provider 不支持该 response_format 时降级到下一个模式
                last_error = exc
                logger.warning("结构化输出模式 %s 不可用: %s", mode, exc)
                continue

            try:
                return model_cls.model_validate_json(raw)
            except ValidationError as exc:
                logger.warning("结构化输出校验失败(%s): %s", mode, exc)
                last_error = exc
                # 交给下一模式重试, 若是 prompt 模式则做一次修复
                if mode == "prompt":
                    repaired = await self._repair(messages, raw, exc, model_cls, temperature)
                    if repaired is not None:
                        return repaired

        raise LLMError(f"结构化输出失败: {last_error}")

    def _structured_modes(self, schema: Dict[str, Any]) -> List[str]:
        if self.structured_mode == "auto":
            return ["json_schema", "json_object", "prompt"]
        if self.structured_mode in ("json_schema", "json_object", "prompt"):
            return [self.structured_mode]
        return ["prompt"]

    @staticmethod
    def _response_format(mode: str, schema: Dict[str, Any], model_cls: Type[BaseModel]) -> Optional[Dict[str, Any]]:
        if mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": model_cls.__name__,
                    "schema": schema,
                    "strict": False,
                },
            }
        if mode == "json_object":
            return {"type": "json_object"}
        return None

    @staticmethod
    def _inject_schema_prompt(messages: List[ChatMessage], schema: Dict[str, Any]) -> List[ChatMessage]:
        instruction = (
            "只输出一个 JSON 对象, 不要包含任何解释文字或 Markdown 代码块标记。\n"
            f"JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
        )
        enriched = list(messages)
        if enriched and enriched[0].role == "system":
            enriched[0] = ChatMessage(
                role="system",
                content=(enriched[0].content or "") + "\n\n" + instruction,
            )
        else:
            enriched.insert(0, ChatMessage(role="system", content=instruction))
        return enriched

    async def _chat_raw(
        self,
        messages: List[ChatMessage],
        response_format: Optional[Dict[str, Any]],
        temperature: float,
    ) -> str:
        message = await self.chat(
            messages,
            response_format=response_format,
            temperature=temperature,
        )
        content = (message.content or "").strip()
        if not content:
            raise LLMError("模型返回了空内容")
        return content

    async def validate_or_repair(
        self,
        messages: List[ChatMessage],
        content: str,
        model_cls: Type[T],
    ) -> T:
        """校验模型输出; 校验失败时把错误回传, 让模型自行修正一次。"""
        try:
            return model_cls.model_validate_json(content)
        except ValidationError as exc:
            logger.warning("结构化输出校验失败: %s", exc)
            repaired = await self._repair(messages, content, exc, model_cls, temperature=0.1)
            if repaired is None:
                raise
            return repaired

    async def _repair(
        self,
        messages: List[ChatMessage],
        raw: str,
        error: ValidationError,
        model_cls: Type[T],
        temperature: float,
    ) -> Optional[T]:
        """把校验错误回传给模型, 让它修正一次。"""
        try:
            repaired = await self.chat(
                list(messages)
                + [
                    ChatMessage(role="assistant", content=raw),
                    ChatMessage(
                        role="user",
                        content=(
                            "上一次的 JSON 校验失败, 错误如下:\n"
                            f"{error}\n"
                            "请只输出修正后的 JSON 对象, 不要任何额外文字。"
                        ),
                    ),
                ],
                temperature=temperature,
            )
            content = (repaired.content or "").strip()
            return model_cls.model_validate_json(content)
        except (ValidationError, LLMError) as exc:
            logger.warning("结构化输出修复失败: %s", exc)
            return None
