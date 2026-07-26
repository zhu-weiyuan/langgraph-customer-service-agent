"""Priority-aware context assembly for LangGraph LLM calls.

JavaGuide Context Engineering best practice: reserve budget fractions per category,
prioritize emotional user content over filler in history, ensure RAG grounding snippets survive truncation.
"""
from __future__ import annotations
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable
from langchain_core.messages import HumanMessage, AIMessage
from .context_monitor import TokenEstimator
from .prompt_registry import PromptRegistry, PromptVersion


_EMOTIONAL_RE = re.compile(
    r'\b(angry|unacceptable|hate|sad|mad|urgent|rude|bad|wrong|'
    r'issue|problem|fix|help|refund|cancel|fire|money back)\b',
    re.IGNORECASE,
)


@dataclass
class ContextPiece:
    label: str
    content: str
    priority: int
    token_estimate: int = 0
    recency: int = 0


@dataclass
class ContextBundle:
    messages: list[dict]
    tool_schema: list[dict]
    metadata: dict[str, Any] = field(default_factory=dict)


class TokenBudgetAllocator:
    def __init__(self, context_window: int = 128_000, reserved_output: int = 4096):
        self.context_window, self.reserved_output = context_window, reserved_output
        self.estimator = TokenEstimator()

    @property
    def full_budget(self) -> int:
        return self.context_window - self.reserved_output

    def _estimate_item_tokens(self, content: str) -> int:
        """Estimated token count. Consistent with simple-agent calibration.

        Formula: chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5
        where english_words = word COUNT from regex (not char count).
        """
        if not content:
            return 0
        chinese_chars = len(re.findall(r'[一-鿿]', content))
        english_words = len(re.findall(r'[a-zA-Z]+', content))
        other_chars = max(0, len(content) - chinese_chars - english_words)
        tokens = int(chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5)
        return max(tokens, 1) if content.strip() else 0

    def _char_to_approx_tokens(self, char_count: int) -> int:
        """Rough char-to-token ratio for head-truncation sizing."""
        return max(1, int(char_count * 0.7))

    def _truncate_head(self, content: str, target_tokens: int) -> str:
        """Truncate from the head — preserves 'Goal:', 'doc:' prefixes."""
        if not content:
            return ""
        target_chars = max(6, self._char_to_approx_tokens(target_tokens))
        if len(content) <= target_chars:
            return content
        return content[:target_chars]

    def _add_piece(self, piece: ContextPiece, available_tokens: int,
                   preserve_head: bool = True) -> tuple[ContextPiece | None, int]:
        """Try to add a piece within budget. Returns (piece or None, tokens_used)."""
        est = piece.token_estimate or self._estimate_item_tokens(piece.content)

        if available_tokens >= est:
            return ContextPiece(
                piece.label, piece.content, piece.priority, est, recency=piece.recency
            ), est

        if available_tokens <= 5:
            return None, 0

        # Truncate to fit
        method = self._truncate_head if preserve_head else self._truncate_tail
        truncated = method(piece.content, available_tokens)
        t_est = self._estimate_item_tokens(truncated)
        if t_est > 0:
            return ContextPiece(
                piece.label, truncated, piece.priority, t_est, recency=piece.recency
            ), t_est
        return None, 0

    def _truncate_tail(self, content: str, target_tokens: int) -> str:
        """Truncate from the tail to fit within target_tokens."""
        if not content:
            return ""
        target_chars = max(6, self._char_to_approx_tokens(target_tokens))
        if len(content) <= target_chars:
            return content
        return content[len(content) - target_chars:]

    def _is_large_filler(self, piece: ContextPiece) -> bool:
        """Detect pure filler — high token count dominated by repeated generic words."""
        est = piece.token_estimate or self._estimate_item_tokens(piece.content)
        if est < 30:
            return False
        text_lower = piece.content.lower()
        # Not filler if it has emotional keywords
        if _EMOTIONAL_RE.search(text_lower):
            return False
        words = re.findall(r'[a-z]+', text_lower)
        if len(words) > 5:
            counter = Counter(words)
            most_common_word, most_common_count = counter.most_common(1)[0]
            if most_common_count >= len(words) * 0.3 and len(most_common_word) < 8:
                return True
        return False

    def _role_from_piece(self, piece: ContextPiece) -> str | None:
        """Extract role from 'user:' or 'assistant:' prefix in piece content."""
        if ":" not in piece.content:
            return None
        prefix = piece.content.split(":", 1)[0].strip().lower()
        if prefix in ("user", "assistant"):
            return prefix
        return None

    def allocate_pieces(self, pieces: Iterable[ContextPiece]) -> list[ContextPiece]:
        """Allocate pieces within budget with category-reserved fractions.

        Strategy (JavaGuide Context Engineering):
        1. System prompt ~5% reserved
        2. Task goal — always included, truncated to fit
        3. RAG — guaranteed slice, preserves prefixes like "doc:"
        4. Memory summary — limited to avoid starving other categories
        5. History: emotional user messages first, then recency, skip filler
        """
        full = self.full_budget
        selected: list[ContextPiece] = []
        used = 0
        avail = full

        all_pieces = list(pieces)

        # Phase 1: System prompt — tiny, guaranteed first slot
        system_piece = next((p for p in all_pieces if p.label == "system"), None)
        if system_piece:
            added, cost = self._add_piece(system_piece, avail)
            if added:
                selected.append(added); used += cost; avail -= cost

        # Phase 2: Task goal — always include (truncated to fit budget)
        task_pool = sorted(
            [p for p in all_pieces if p.label == "task"],
            key=lambda p: (-p.priority, -p.recency),
        )
        if task_pool and task_pool[0].content.strip():
            # Reserve up to 25% of full budget for task goal (important for traceability)
            task_budget = max(20, int(full * 0.25))
            added, cost = self._add_piece(task_pool[0], task_budget, preserve_head=True)
            if added:
                selected.append(added); used += cost; avail -= cost

        # Phase 3: RAG — guarantee at least min of remaining or 40% for grounding
        rag_pool = sorted(
            [p for p in all_pieces if p.label == "rag"],
            key=lambda p: (-p.priority, -p.recency),
        )
        if rag_pool and avail > 15:
            # Guarantee at least 40% of remaining budget (or a minimum) for RAG grounding
            rag_guarantee = max(30, min(avail // 2, int(full * 0.40)))
            for rag_piece in rag_pool:
                if avail <= 10:
                    break
                added, cost = self._add_piece(rag_piece, min(avail, rag_guarantee), preserve_head=True)
                if added:
                    selected.append(added); used += cost; avail -= cost

        # Phase 4: Memory summary — limited fraction to avoid starving RAG
        mem_pool = sorted(
            [p for p in all_pieces if p.label == "memory"],
            key=lambda p: (-p.priority, -p.recency),
        )
        if mem_pool and mem_pool[0].content.strip():
            # Cap memory to 15% of full budget max (prevents starving RAG)
            mem_cap = min(avail, max(20, int(full * 0.15)))
            added, cost = self._add_piece(mem_pool[0], mem_cap)
            if added:
                selected.append(added); used += cost; avail -= cost

        # Phase 5: History — emotional user messages first, then recency, skip pure filler
        history_prio = sorted(
            [p for p in all_pieces if p.label == "history"],
            key=lambda p: (-p.recency, -p.priority),
        )
        for p in history_prio:
            if used >= avail or avail <= 5:
                break

            est_tokens = p.token_estimate or self._estimate_item_tokens(p.content)

            # Skip empty pieces entirely
            if est_tokens == 0 and not p.content.strip():
                continue

            # Skip large filler aggressively when budget is tight
            if self._is_large_filler(p) and avail < full * 0.5:
                continue

            added, cost = self._add_piece(p, avail)
            if added:
                selected.append(added); used += cost; avail -= cost

        return selected


class ContextAssembler:
    """Progressively disclose memory/RAG metadata, then compact to the context budget."""

    def __init__(self, registry: PromptRegistry | None = None,
                 allocator: TokenBudgetAllocator | None = None):
        self.registry = registry or PromptRegistry()
        self.allocator = allocator or TokenBudgetAllocator()
        if not self.registry._versions.get("system"):
            self.registry.register("system", "You are a helpful customer service assistant.")

    def assemble(self, state: dict, user_message: str, session_id: str = "") -> ContextBundle:
        prompt = self.registry.get("system")

        # Build pieces in strict priority order
        pieces: list[ContextPiece] = [ContextPiece("system", prompt.content, 100)]

        # Task goal — always include 'task_goal:' literal prefix for traceability/metadata
        goal = state.get("task_goal")
        constraints = state.get("constraints") or []
        if goal or (constraints and constraints != []):
            parts = ["Goal: " + str(goal or "")]
            if constraints and constraints != []:
                constraint_str = "; ".join(map(str, constraints))
                if constraint_str:
                    parts.append("Constraints: " + constraint_str)
            pieces.append(ContextPiece("task", "\n".join(parts), 90))

        # Memory summary
        memory = state.get("memory_summary") or state.get("memory")
        if memory and str(memory).strip():
            pieces.append(ContextPiece("memory", str(memory), 60))

        # RAG results — sort by relevance score desc, then title length asc for precision
        rag_results = state.get("rag_results") or []
        if isinstance(rag_results, dict):
            rag_results = [rag_results]

        relevant_rag = [r for r in rag_results if r.get("relevant", False)]
        relevant_rag.sort(key=lambda x: (-x.get("score", 0.5), len(x.get("title", ""))))

        # Build RAG pieces with score-based priority (limit to top relevant items)
        for idx, item in enumerate(relevant_rag[:4]):
            if isinstance(item, dict):
                title = item.get("title", "evidence")
                content_val = item.get("content", "") or title
                priority_score = 70 + int(item.get("score", 0.5) * 10)
                pieces.append(ContextPiece(
                    "rag",
                    f"{title}: {content_val}",
                    priority_score,
                    recency=4 - idx,
                ))
            else:
                pieces.append(ContextPiece("rag", str(item), 70, recency=4 - idx))

        # History messages — compute combined score for sorting/selection
        history = state.get("messages", [])
        max_index = len(history) * 10 + 500

        for index, item in enumerate(history):
            content_val = getattr(item, "content", "") if hasattr(item, "content") else None
            if content_val is None:
                content_val = item.get("content", "") if isinstance(item, dict) else str(item)

            role = "assistant" if isinstance(item, AIMessage) else "user"
            prefix = f"{role}: {content_val}"

            # Boost emotional user messages with high priority and recency
            is_emotional_user = (bool(_EMOTIONAL_RE.search(content_val))
                               and role == "user")

            priority = 50 if is_emotional_user else 40
            combined_recency = (max_index - index * 10) + (1000 if is_emotional_user else 0)

            pieces.append(ContextPiece(
                "history", prefix, priority, recency=combined_recency
            ))

        # Allocate across budget using JavaGuide strategy
        selected = self.allocator.allocate_pieces(pieces)

        # Build system prompt from non-history pieces with strict priority ordering
        system_parts = [prompt.content]

        for piece in selected:
            if piece.label == "task":
                raw_content = piece.content
                if "Goal: " in raw_content or "task_goal:" in raw_content:
                    # Preserve 'task_goal:' label so trace_id/metadata references work,
                    # then include the goal text for LLM parsing.
                    system_parts.append(f"task_goal:{raw_content.strip()}")

        for piece in selected:
            if piece.label == "memory" and piece.content.strip():
                system_parts.append(f"Memory Context: {piece.content}")

        for piece in selected:
            if piece.label == "rag" and piece.content.strip():
                system_parts.append(piece.content)

        # Surface recent emotional user messages into system prompt
        # (JavaGuide: urgent content must be visible to LLM, not buried in history)
        emotional_msgs = []
        for piece in selected:
            if piece.label == "history":
                role = self.allocator._role_from_piece(piece)
                if role == "user" and _EMOTIONAL_RE.search(piece.content):
                    msg_text = piece.content.split(": ", 1)[-1] if ": " in piece.content else piece.content.strip()
                    emotional_msgs.append(msg_text)

        # Show up to 2 most recent emotional user messages in system prompt
        if emotional_msgs:
            system_parts.append("Recent User Messages: " + "; ".join(emotional_msgs[-2:]))

        system_content = "\n\n".join(system_parts)

        # Collect history messages in their original order (for conversation context)
        history_messages: list[dict] = []
        for piece in selected:
            if piece.label == "history":
                try:
                    role_part, content_part = piece.content.split(":", 1)
                    role_prefix = role_part.strip().lower()
                    if role_prefix in ["user", "assistant"]:
                        history_messages.append({
                            "role": role_prefix,
                            "content": content_part.strip()
                        })
                except ValueError:
                    continue

        # Build final message list: system -> user query -> history messages
        final_messages: list[dict] = []
        final_messages.append({"role": "system", "content": system_content})
        final_messages.append({"role": "user", "content": user_message})
        for msg in history_messages:
            final_messages.append(msg)

        tools = state.get("available_tools") or []

        return ContextBundle(
            final_messages, tools, {
                "source_counts": {
                    label: sum(p.label == label for p in selected)
                    for label in {p.label for p in selected}
                },
                "token_estimate": sum(p.token_estimate for p in selected),
                "prompt_version": f"{prompt.name}:{prompt.version_no}",
                "session_id": session_id,
            }
        )
