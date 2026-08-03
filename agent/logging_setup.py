"""
agent/logging_setup.py — 结构化日志 (P3 可观测性栈)

- 优先 structlog (守卫导入); 降级 stdlib logging.Formatter, 输出单行 JSON:
  {ts, level, logger, msg, request_id, session_id, ...}
- contextvar `request_id_var` / `session_id_var`: 在中间件里绑定一次,
  之后该请求上下文中所有日志自动携带。

用法 (app 层, 启动时调用一次):
    from agent.logging_setup import setup_logging, bind_request_context
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    # 中间件内:
    token = bind_request_context(request_id=rid, session_id=sid)
    try:
        ...
    finally:
        clear_request_context(token)
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple

# ── contextvars: request/session 关联 ID 传播 ─────────────────
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="")
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="")


def bind_request_context(request_id: str = "", session_id: str = "") -> Tuple:
    """绑定当前上下文的 request_id/session_id, 返回 tokens 供 clear 使用。"""
    t1 = request_id_var.set(request_id) if request_id else None
    t2 = session_id_var.set(session_id) if session_id else None
    return (t1, t2)


def clear_request_context(tokens: Optional[Tuple] = None) -> None:
    """请求结束时清理绑定。tokens 为 bind_request_context 的返回值。"""
    if tokens:
        t1, t2 = tokens
        if t1 is not None:
            request_id_var.reset(t1)
        if t2 is not None:
            session_id_var.reset(t2)
    else:
        request_id_var.set("")
        session_id_var.set("")


def get_request_id() -> str:
    return request_id_var.get()


def get_session_id() -> str:
    return session_id_var.get()


# ── 可选依赖: structlog ───────────────────────────────────────
try:  # pragma: no cover - 环境相关
    import structlog  # type: ignore
    _HAS_STRUCTLOG = True
except Exception:  # pragma: no cover
    structlog = None
    _HAS_STRUCTLOG = False


# ── stdlib 降级: 单行 JSON Formatter ──────────────────────────
class JsonFormatter(logging.Formatter):
    """单行 JSON 日志: ts/level/logger/msg/request_id/session_id。"""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "session_id": session_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # 透传 extra 字段
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and k not in payload and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def _structlog_context_processor(logger_, method_name, event_dict):
    """structlog processor: 注入 contextvar 中的 request_id/session_id。"""
    event_dict.setdefault("request_id", request_id_var.get())
    event_dict.setdefault("session_id", session_id_var.get())
    return event_dict


def setup_logging(level: str = "INFO", force_stdlib: bool = False) -> logging.Logger:
    """配置全局结构化日志。app 启动时调用一次。

    Args:
        level: DEBUG/INFO/WARNING/ERROR
        force_stdlib: True 时跳过 structlog (测试降级路径用)
    Returns:
        root logger
    """
    # P1 — Windows GBK 编码容错：日志中含非 GBK 字符（如 \u2011 不断连短横）
    # 时 StreamHandler.emit() 抛 UnicodeEncodeError 并静默丢弃整条消息。
    # reconfigure(errors='replace') 让 stdout/stderr 用 '?' 替换无法编码的字符
    # 而非抛出异常，保证日志行永远可达。
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(errors='replace')

    log_level = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if _HAS_STRUCTLOG and not force_stdlib:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                _structlog_context_processor,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.EventRenamer("msg"),
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            cache_logger_on_first_use=True,
        )
        # stdlib 侧仍走 JSON formatter, 保证第三方库日志也是单行 JSON
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(JsonFormatter())

    root.addHandler(handler)
    return root


def get_logger(name: str = "agent"):
    """获取 logger: structlog 可用时返回 structlog logger, 否则 stdlib。"""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


__all__ = [
    "request_id_var", "session_id_var",
    "bind_request_context", "clear_request_context",
    "get_request_id", "get_session_id",
    "setup_logging", "get_logger", "JsonFormatter",
]
