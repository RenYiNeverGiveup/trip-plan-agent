"""工具注册表与 JSON Schema 自动推断。

框架不要求开发者手写 JSON Schema: 只要给函数写好类型注解和 docstring,
`build_tool` 就能自动推断出参数结构, 并生成 OpenAI function calling 所需的 schema。
"""

import inspect
import typing
from typing import Any, Callable, Dict, List, Optional, get_args, get_origin

from .types import ToolSpec

# Python 类型 -> JSON Schema 类型
_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _strip_optional(annotation: Any) -> Any:
    """Optional[X] / Union[X, None] -> X, 并标记是否可选。"""
    origin = get_origin(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        return Any, True
    return annotation, False


def _schema_for_annotation(annotation: Any) -> Dict[str, Any]:
    """把 Python 注解转换成 JSON Schema 片段。"""
    annotation, _ = _strip_optional(annotation)
    if annotation in _JSON_TYPES:
        schema = {"type": _JSON_TYPES[annotation]}
        if annotation is list:
            schema["items"] = {"type": "string"}
        return schema

    origin = get_origin(annotation)
    if origin in (list, List):
        args = get_args(annotation)
        item = args[0] if args else str
        return {"type": "array", "items": _schema_for_annotation(item)}
    if origin in (dict, Dict):
        return {"type": "object"}

    return {"type": "string"}


def build_tool(
    name: str,
    description: str,
    func: Callable[..., Any],
    *,
    doc_description: Optional[str] = None,
) -> ToolSpec:
    """根据函数签名自动构造 ToolSpec。

    Args:
        name: 工具名(给模型看的, 需唯一)
        description: 工具用途描述, 会直接进入 prompt, 写清楚能显著提高调用准确率
        func: 实际执行的函数, 支持同步与异步
        doc_description: 覆盖 description(一般直接用函数 docstring 更自然)
    """
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        annotation = (
            param.annotation
            if param.annotation is not inspect.Parameter.empty
            else str
        )
        _, optional = _strip_optional(annotation)
        schema = _schema_for_annotation(annotation)
        if param.default is not inspect.Parameter.empty:
            optional = True
            if isinstance(param.default, (int, float, bool)):
                schema["default"] = param.default
            elif isinstance(param.default, str):
                schema["default"] = param.default

        properties[param_name] = schema
        if not optional:
            required.append(param_name)

    parameters: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    summary = doc.split("\n\n")[0].strip() if doc else ""

    return ToolSpec(
        name=name,
        description=doc_description or summary or description,
        parameters=parameters,
        handler=func,
    )


class ToolRegistry:
    """工具注册表: 统一管理工具声明、查找与 schema 导出。"""

    def __init__(self, tools: Optional[List[ToolSpec]] = None) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        for spec in tools or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def register_all(self, specs: List[ToolSpec]) -> "ToolRegistry":
        for spec in specs:
            self.register(spec)
        return self

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def specs(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        return [spec.to_openai() for spec in self._tools.values()]
