"""
Structured logging configuration.

Outputs JSON-formatted logs for easy parsing by ELK/CloudWatch.
Replaces all print() calls with proper logging.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""
    
    def format(self, record):
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, 'session_id'):
            log_data["session_id"] = record.session_id
        if hasattr(record, 'user_message'):
            log_data["user_message"] = record.user_message[:200]  # Truncate
        
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(level: str = "INFO", console_output: bool = True):
    """Configure structured logging.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        console_output: If True, output to stdout; else file only
    """
    # P1 — Windows GBK 编码容错: 日志中含非 GBK 字符（如 \u2011 不断连短横）
    # 时 StreamHandler.emit() 抛 UnicodeEncodeError 并静默丢弃整条消息。
    # reconfigure(errors='replace') 让 stdout/stderr 用 '?' 替换无法编码的
    # 字符而非抛出异常, 保证日志行永远可达。
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(errors='replace')

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler with JSON formatter
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(JsonFormatter())
        root_logger.addHandler(console_handler)
    
    return root_logger


# Pre-configured logger for customer service agent
logger = setup_logging("INFO")
