"""Small startup-loaded prompt registry with rendering validation and audit versions."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version_no: int
    content: str
    variables_schema: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    change_reason: str = "startup load"


class PromptRegistry:
    """In-memory immutable version history; intentionally simple for first rollout."""
    _VARIABLE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")

    def __init__(self):
        self._versions: Dict[str, list[PromptVersion]] = {}

    def register(self, name: str, content: str, *, variables_schema=(),
                 change_reason: str = "startup load") -> PromptVersion:
        required = tuple(variables_schema) or tuple(sorted(set(self._VARIABLE.findall(content))))
        existing = self._versions.setdefault(name, [])
        prompt = PromptVersion(name, len(existing) + 1, content, required,
                               change_reason=change_reason)
        existing.append(prompt)
        return prompt

    def load(self, name: str, *, file_path: Optional[str] = None,
             env_var: Optional[str] = None, default: Optional[str] = None,
             variables_schema=(), change_reason: str = "startup load") -> PromptVersion:
        content = os.getenv(env_var) if env_var else None
        if content is None and file_path:
            content = Path(file_path).read_text(encoding="utf-8")
        if content is None:
            content = default
        if content is None:
            raise ValueError(f"No prompt content configured for {name}")
        return self.register(name, content, variables_schema=variables_schema, change_reason=change_reason)

    def get(self, name: str, version_no: Optional[int] = None) -> PromptVersion:
        versions = self._versions.get(name, [])
        if not versions:
            raise KeyError(f"Unknown prompt: {name}")
        return versions[-1] if version_no is None else versions[version_no - 1]

    def render(self, name: str, variables: Mapping[str, object], *, version_no: Optional[int] = None) -> tuple[str, PromptVersion]:
        prompt = self.get(name, version_no)
        missing = [key for key in prompt.variables_schema if key not in variables]
        if missing:
            raise ValueError(f"Missing required prompt variables for {name}: {', '.join(missing)}")
        return prompt.content.format(**variables), prompt

    def render_and_validate(self, system_prompt_template: str, user_context: Mapping[str, object]) -> str:
        """Render an ad-hoc system template with the same strict validation as registry prompts."""
        required = tuple(sorted(set(self._VARIABLE.findall(system_prompt_template))))
        missing = [key for key in required if key not in user_context]
        if missing:
            raise ValueError("Missing required prompt variables: " + ", ".join(missing))
        try:
            return system_prompt_template.format(**user_context)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid prompt rendering: {exc}") from exc
