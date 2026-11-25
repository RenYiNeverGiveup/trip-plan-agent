"""多智能体编排: 并行执行 + 结果聚合。

旅行规划中的"找景点 / 查天气 / 找酒店"三件事彼此独立, 串行执行会把延迟
放大三倍。这里用 asyncio.gather 并行跑, 并对单个任务做异常隔离。
"""

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Tuple, Union

logger = logging.getLogger(__name__)


class OrchestrationError(RuntimeError):
    """所有子任务都失败。"""


async def run_parallel(
    tasks: Dict[str, Union[Callable[[], Awaitable[Any]], Awaitable[Any]]],
    *,
    return_exceptions: bool = True,
) -> Dict[str, Any]:
    """并行执行多个异步任务。

    Args:
        tasks: 名称 -> 零参协程工厂, 或直接给出协程对象
        return_exceptions: True 时把异常作为结果返回, False 时首个异常直接抛出

    Returns:
        名称 -> 执行结果(或异常对象)
    """
    names = list(tasks.keys())
    coroutines = []
    for name in names:
        value = tasks[name]
        if not inspect.isawaitable(value):
            value = value()
        coroutines.append(value)

    results = await asyncio.gather(*coroutines, return_exceptions=return_exceptions)

    output: Dict[str, Any] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning("子任务 %s 失败: %s", name, result)
        else:
            logger.info("子任务 %s 完成", name)
        output[name] = result
    return output


async def run_all_successful(results: Dict[str, Any]) -> Dict[str, Any]:
    """过滤掉失败的子任务; 若全部失败则抛异常。"""
    ok = {name: value for name, value in results.items() if not isinstance(value, BaseException)}
    if not ok:
        errors = "; ".join(f"{k}: {v}" for k, v in results.items() if isinstance(v, BaseException))
        raise OrchestrationError(f"所有子任务均失败 -> {errors}")
    return ok
