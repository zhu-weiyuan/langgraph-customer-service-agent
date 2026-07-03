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
