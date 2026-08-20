"""Tool permission registry used by agent integrations before invoking side effects."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ToolRiskLevel(str, Enum):
    TOOL_READ = "TOOL_READ"
    TOOL_WRITE_LOW = "TOOL_WRITE_LOW"
    TOOL_WRITE_HIGH = "TOOL_WRITE_HIGH"


@dataclass(frozen=True)
class ToolCallContext:
    confirmed: bool = False
    request_id: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    handler: Callable[..., Any]
    risk_level: ToolRiskLevel = ToolRiskLevel.TOOL_READ


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, RegisteredTool] = {}

    def register(self, name: Optional[str] = None, *, risk_level: ToolRiskLevel = ToolRiskLevel.TOOL_READ):
        """Decorator or direct registration factory for a tool handler."""
        def decorator(handler: Callable[..., Any]):
            tool_name = name or handler.__name__
            self._tools[tool_name] = RegisteredTool(tool_name, handler, ToolRiskLevel(risk_level))
            return handler
        return decorator

    def get(self, name: str) -> RegisteredTool:
        return self._tools[name]

    def execute(self, name: str, *args: Any, context: Optional[ToolCallContext] = None, **kwargs: Any) -> Any:
        tool = self.get(name)
        context = context or ToolCallContext()
        if tool.risk_level == ToolRiskLevel.TOOL_WRITE_HIGH:
            logger.warning("High-risk tool audit: tool=%s request_id=%s user_id=%s confirmed=%s",
                           name, context.request_id, context.user_id, context.confirmed)
            if not context.confirmed:
                raise PermissionError(f"High-risk tool '{name}' requires explicit confirmation")
        return tool.handler(*args, **kwargs)
