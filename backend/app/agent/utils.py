"""Agent 框架通用工具函数。"""

import json
import re
from typing import Any, Dict, List

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """从模型输出中提取 JSON 文本。

    依次尝试: 直接解析 -> 去掉 ``` 代码块 -> 截取第一个 { 到最后一个 }
    这样即便模型不听话地加了前言或代码块标记, 也能拿到可解析的 JSON。
    """
    if not text:
        raise ValueError("模型返回了空内容")

    candidate = text.strip()
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    match = _FENCE_RE.search(candidate)
    if match:
        fenced = match.group(1).strip()
        try:
            json.loads(fenced)
            return fenced
        except json.JSONDecodeError:
            candidate = fenced

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        sliced = candidate[start : end + 1]
        json.loads(sliced)  # 校验, 不合法则抛错
        return sliced

    raise ValueError("未能从模型输出中解析出 JSON")


def json_dumps(data: Any) -> str:
    """统一的项目内 JSON 序列化(中文不转义)。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def chunked(items: List[Any], size: int) -> List[List[Any]]:
    """把列表按固定大小切分。"""
    return [items[i : i + size] for i in range(0, len(items), size)]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def truncate_catalog(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """限制送给模型的候选数量, 控制 prompt 长度。"""
    return items[:limit]
