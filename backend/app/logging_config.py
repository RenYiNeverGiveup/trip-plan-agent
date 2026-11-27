"""统一日志配置。

解决三个问题:
1. **看不出是谁打的日志** —— 标准 logging 默认格式没有类名。这里用 `ClassNameFilter`
   在日志调用时回溯调用栈, 找到 `self` 所属的类, 注入 `record.class_name`,
   配合 `%(filename)s:%(lineno)d` 就能精确定位到"哪个类的哪一行"。
   *不需要修改任何现有 `logger.info(...)` 调用点*。

2. **第三方 API 调用是黑盒** —— 提供 `log_http_request` / `log_http_response`
   与 `log_llm_request` / `log_llm_response`, 统一打印入参与出参(摘要)。

3. **密钥会打进日志** —— 所有落盘/输出的参数都经过 `mask_secrets` 脱敏,
   key / token / secret / Authorization 一律替换为 `***`。
"""

import inspect
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------- #
# 脱敏与截断
# ---------------------------------------------------------------------- #

#: 键名中包含以下关键字时, 值会被替换为 ***
_SECRET_KEY_PATTERNS = ("key", "token", "secret", "password", "passwd", "authorization", "appkey")

#: 日志中单条文本的最大长度
MAX_LOG_LENGTH = 2000

_MASK = "***"


def _is_secret_key(name: str) -> bool:
    lowered = str(name).lower()
    # "keyword" / "monkey" 之类不算密钥
    return any(pattern in lowered for pattern in _SECRET_KEY_PATTERNS) and lowered not in (
        "keywords",
        "keyword",
    )


def mask_secrets(obj: Any) -> Any:
    """递归脱敏字典中的密钥字段。"""
    if isinstance(obj, dict):
        return {
            key: (_MASK if _is_secret_key(key) else mask_secrets(value))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [mask_secrets(item) for item in obj]
    return obj


def mask_url(url: str) -> str:
    """把 URL 查询串里的 key/token 等敏感参数替换掉。"""
    if not url:
        return url

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if _is_secret_key(name):
            return f"{name}={_MASK}"
        return match.group(0)

    return re.sub(r"([A-Za-z_][A-Za-z0-9_]*)=([^&\s]+)", _replace, url)


def truncate(text: Any, limit: int = MAX_LOG_LENGTH) -> str:
    """把过长的文本截断, 并标注被截断的长度。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(mask_secrets(text), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<已截断, 共 {len(text)} 字符>"


def dumps(obj: Any, limit: int = MAX_LOG_LENGTH) -> str:
    """安全地把对象转成可读字符串(脱敏 + 截断)。"""
    if isinstance(obj, str):
        return truncate(obj, limit)
    try:
        return truncate(json.dumps(mask_secrets(obj), ensure_ascii=False, default=str), limit)
    except (TypeError, ValueError):
        return truncate(str(obj), limit)


# ---------------------------------------------------------------------- #
# 类名注入
# ---------------------------------------------------------------------- #

#: 这些模块的帧在回溯时跳过(否则会误判成 Logger / Handler / Filter)
_SKIP_MODULES = {"logging", "logging.handlers", __name__}


def find_class_name(max_depth: int = 20) -> str:
    """回溯调用栈, 找到最近一个带 `self` 的帧并返回其类名。

    模块级函数(没有 self)返回 "-"。
    """
    frame = inspect.currentframe()
    try:
        depth = 0
        while frame is not None and depth < max_depth:
            module_name = frame.f_globals.get("__name__", "")
            if module_name not in _SKIP_MODULES:
                obj = frame.f_locals.get("self")
                if obj is not None:
                    return type(obj).__name__
            frame = frame.f_back
            depth += 1
    finally:
        del frame
    return "-"


class ClassNameFilter(logging.Filter):
    """给每条日志注入 `class_name` 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.class_name = find_class_name()
        return True


# ---------------------------------------------------------------------- #
# 日志格式与初始化
# ---------------------------------------------------------------------- #

#: 控制台格式: 时间 | 级别 | logger | 类名 | 文件:行号 | 函数 | 消息
CONSOLE_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(class_name)s | "
    "%(filename)s:%(lineno)d | %(funcName)s | %(message)s"
)

#: 文件格式(与控制台一致, 便于 grep)
FILE_FORMAT = CONSOLE_FORMAT

#: 默认日志目录(相对 backend 目录)
DEFAULT_LOG_DIR = "logs"


def setup_logging(
    level: Optional[str] = None,
    log_dir: Optional[str] = None,
    to_file: bool = True,
) -> None:
    """初始化根 logger。

    Args:
        level: 日志级别, 默认读 `LOG_LEVEL` 环境变量, 兜底 INFO
        log_dir: 日志目录, 默认 `backend/logs`
        to_file: 是否同时写入滚动文件
    """
    resolved_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    # Windows 控制台默认不是 UTF-8, 中文日志会变成乱码, 这里统一改成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 某些环境不支持重配置
            pass

    formatter = logging.Formatter(FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    class_filter = ClassNameFilter()

    root = logging.getLogger()
    # 清掉已有的 handler, 避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(resolved_level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(class_filter)
    root.addHandler(console)

    if to_file:
        directory = log_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), DEFAULT_LOG_DIR)
        try:
            os.makedirs(directory, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.join(directory, "app.log"),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(class_filter)
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - 磁盘不可写时降级为仅控制台
            root.warning("日志文件创建失败, 仅输出到控制台: %s", exc)

    # 第三方库的噪音日志降级
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------- #
# 第三方调用日志
# ---------------------------------------------------------------------- #


def log_http_request(logger: logging.Logger, method: str, url: str, params: Any = None) -> None:
    """打印第三方 HTTP 请求(入参)。"""
    logger.info(
        "→ HTTP %s %s params=%s",
        method.upper(),
        mask_url(str(url)),
        dumps(params, 500),
    )


def log_http_response(
    logger: logging.Logger,
    method: str,
    url: str,
    status_code: int,
    body: Any = None,
    elapsed_ms: Optional[float] = None,
) -> None:
    """打印第三方 HTTP 响应(出参)。"""
    logger.info(
        "← HTTP %s %s status=%s elapsed=%s body=%s",
        method.upper(),
        mask_url(str(url)),
        status_code,
        f"{elapsed_ms:.0f}ms" if elapsed_ms is not None else "-",
        dumps(body, 800),
    )


def log_llm_request(
    logger: logging.Logger,
    model: str,
    messages: Any,
    tools: Any = None,
    extra: Any = None,
) -> None:
    """打印 LLM 请求(入参)。"""
    logger.info(
        "→ LLM 请求 model=%s messages=%s tools=%s extra=%s",
        model,
        dumps(messages, 1200),
        dumps(tools, 300),
        dumps(extra, 300),
    )


def log_llm_response(
    logger: logging.Logger,
    model: str,
    content: Any = None,
    tool_calls: Any = None,
    usage: Any = None,
    elapsed_ms: Optional[float] = None,
) -> None:
    """打印 LLM 响应(出参)。"""
    logger.info(
        "← LLM 响应 model=%s elapsed=%s usage=%s tool_calls=%s content=%s",
        model,
        f"{elapsed_ms:.0f}ms" if elapsed_ms is not None else "-",
        dumps(usage, 200),
        dumps(tool_calls, 400),
        dumps(content, 800),
    )


class Timer:
    """简易计时器, 用于统计耗时。"""

    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.start
