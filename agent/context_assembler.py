"""Priority-aware context assembly for the customer-service LLM calls.

Design principles (P1-A rewrite)
--------------------------------
1. **Single token source** — all estimation goes through
   :mod:`agent.token_estimator` (never a private formula).
2. **Physical partition order is sorted by change frequency (ascending)** so
   stable prefixes maximise provider KV-cache hits:

   ============================  ==========================
   partition                     change frequency
   ============================  ==========================
   static system prompt          nearly never
   task goal / constraints       rarely
   tool schemas                  rarely
   RAG evidence + memory         per turn
   conversation history          grows per turn (old→new)
   current user message          every turn — ALWAYS LAST
   ============================  ==========================

   The final message list is therefore::

       [system(static + task + tools + rag/memory)] +
       [history old→new as proper role messages] +
       [current user message]      # the current question is last

3. **rank / fit are decoupled**: :meth:`TokenBudgetAllocator.rank` orders
   pieces by importance; :meth:`TokenBudgetAllocator.fit` packs the ranked
   pieces into the budget with a 3-tier degradation ladder
   (``full`` → ``summary`` → ``reference``). Degraded pieces always keep
   their ``source_id`` so the evidence chain survives compression.
4. **Target utilisation 40–60%** of the usable window. Exceeding 60%
   triggers tier-degradation; the packer never exceeds the 60% ceiling.
5. **Explicit roles** — every piece carries a ``role`` field. Roles are
   never recovered by string-splitting on ``":"``.
6. **RAG field alignment** — nodes.py passes agentic-RAG dicts shaped
   ``{"sufficient": bool, "context": str, "rounds": int, "queries_tried": [...]}``.
   The old code read ``r["relevant"]`` / ``r["content"]``, so RAG results
   never reached the prompt. Both the agentic shape and the legacy
   ``{"title", "content", "score", "relevant"}`` shape are now supported.
"""
from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .token_estimator import estimate_tokens
from .prompt_registry import PromptRegistry

_EMOTIONAL_RE = re.compile(
    r'\b(angry|unacceptable|hate|sad|mad|urgent|rude|bad|wrong|'
    r'issue|problem|fix|help|refund|cancel|fire|money back)\b',
    re.IGNORECASE,
)

# Tier names for the fit ladder.
TIER_FULL = "full"
TIER_SUMMARY = "summary"
TIER_REFERENCE = "reference"


@dataclass
class ContextPiece:
    label: str                    # system | task | tools | rag | memory | history | current
    content: str
    priority: int                 # higher = more important
    role: str = "system"          # explicit message role: system | user | assistant
    source_id: str = ""           # evidence-chain id, survives degradation
    recency: int = 0              # for history: original index (ascending = older→newer)
    token_estimate: int = 0
    tier: str = TIER_FULL
    # True when the piece is only a retrieval trace/metadata stub, not answer evidence.
    metadata_only: bool = False

    def estimated(self) -> int:
        if not self.token_estimate:
            self.token_estimate = estimate_tokens(self.content)
        return self.token_estimate


@dataclass
class ContextBundle:
    messages: list[dict]
    tool_schema: list[dict]
    metadata: dict[str, Any] = field(default_factory=dict)


class TokenBudgetAllocator:
    """Ranks and fits context pieces into a 40–60% utilisation target."""

    TARGET_LOW = 0.40    # below this we are comfortably under budget
    TARGET_HIGH = 0.60   # crossing this triggers tier degradation
    # Minimum tokens worth spending on a summary / a reference stub.
    _MIN_SUMMARY_TOKENS = 24
    _MIN_REFERENCE_TOKENS = 6

    def __init__(self, context_window: int = 128_000, reserved_output: int = 4096):
        self.context_window = context_window
        self.reserved_output = reserved_output

    # ── budget arithmetic ────────────────────────────────────────────
    @property
    def full_budget(self) -> int:
        return max(1, self.context_window - self.reserved_output)

    @property
    def usable_budget(self) -> int:
        """Hard ceiling: never pack beyond TARGET_HIGH of the full budget."""
        return max(1, int(self.full_budget * self.TARGET_HIGH))

    # ── token → char sizing (clearly named; direction is explicit) ───
    @staticmethod
    def tokens_to_approx_chars(tokens: int) -> int:
        """Approximate how many characters fit in ``tokens``.

        Inverse of the heuristic's densest coefficient (CJK ≈ 0.7
        tokens/char → ≈ 1.4 chars/token). Deliberately conservative so a
        truncated string re-estimates at or below the target.
        """
        return max(6, int(tokens * 1.4))

    def _truncate_to_tokens(self, content: str, target_tokens: int,
                            preserve_head: bool = True) -> str:
        """Cut ``content`` so its estimate is <= ``target_tokens``.

        preserve_head=True keeps the beginning (protects "Goal:", "doc:"
        prefixes); False keeps the tail (recent end of a long message).
        """
        if not content:
            return ""
        chars = self.tokens_to_approx_chars(target_tokens)
        cut = content[:chars] if preserve_head else content[-chars:]
        # verify-and-shrink loop: the char approximation may still be over
        while len(cut) > 6 and estimate_tokens(cut) > target_tokens:
            keep = max(6, int(len(cut) * 0.85))
            cut = cut[:keep] if preserve_head else cut[-keep:]
        return cut

    # ── rank ─────────────────────────────────────────────────────────
    def rank(self, pieces: Iterable[ContextPiece]) -> list[ContextPiece]:
        """Order pieces by packing importance (does NOT touch the budget).

        system and the current user message are pinned to the front so they
        are always packed first. Everything else sorts by priority, then by
        recency (newer history first for *selection*; the chronological
        rendering order is restored later from ``recency``).
        """
        pinned, rest = [], []
        for p in pieces:
            (pinned if p.label in ("system", "current") else rest).append(p)
        pinned.sort(key=lambda p: -p.priority)
        rest.sort(key=lambda p: (-p.priority, -p.recency))
        return pinned + rest

    # ── fit ──────────────────────────────────────────────────────────
    def fit(self, ranked: Iterable[ContextPiece]) -> list[ContextPiece]:
        """Pack ranked pieces into the usable budget with 3-tier degradation.

        Ladder per piece: full text → summary (head-truncated, marked) →
        reference-only stub. The loop terminates when the *remaining*
        budget is exhausted (``avail <= 0``) — NOT the old buggy
        ``used >= avail`` comparison, which aborted packing as soon as
        usage crossed ~50% of the budget.
        """
        selected: list[ContextPiece] = []
        avail = self.usable_budget

        for piece in ranked:
            if avail <= 0:
                break  # budget genuinely exhausted
            if not piece.content.strip():
                continue

            est = piece.estimated()

            if est <= avail:                              # tier 1: full
                selected.append(replace(piece, token_estimate=est, tier=TIER_FULL))
                avail -= est
                continue

            # Never degrade the pinned essentials to a stub: shrink instead.
            if piece.label in ("system", "current"):
                cut = self._truncate_to_tokens(piece.content, avail,
                                               preserve_head=piece.label == "system")
                if cut.strip():
                    est = estimate_tokens(cut)
                    selected.append(replace(piece, content=cut,
                                            token_estimate=est, tier=TIER_SUMMARY))
                    avail -= est
                continue

            if avail >= self._MIN_SUMMARY_TOKENS:         # tier 2: summary
                degraded = self._to_summary(piece, avail)
                if degraded is not None:
                    selected.append(degraded)
                    avail -= degraded.token_estimate
                continue

            if avail >= self._MIN_REFERENCE_TOKENS:       # tier 3: reference
                stub = self._to_reference(piece)
                est = estimate_tokens(stub.content)
                if est <= avail:
                    stub.token_estimate = est
                    selected.append(stub)
                    avail -= est
            # else: this piece is skipped; keep scanning — smaller pieces
            # later in the ranking may still fit the remaining budget.

        return selected

    def _to_summary(self, piece: ContextPiece, budget: int) -> ContextPiece | None:
        """Tier 2: head-truncate and mark; keeps source_id for evidence chain."""
        marker = f" …[truncated; source={piece.source_id or piece.label}]"
        body_budget = max(8, budget - estimate_tokens(marker))
        cut = self._truncate_to_tokens(piece.content, body_budget, preserve_head=True)
        if not cut.strip():
            return None
        content = cut + marker
        est = estimate_tokens(content)
        if est > budget:
            return None
        return replace(piece, content=content, token_estimate=est, tier=TIER_SUMMARY)

    @staticmethod
    def _to_reference(piece: ContextPiece) -> ContextPiece:
        """Tier 3: citation-only stub; the source_id IS the payload."""
        ref = f"[ref:{piece.source_id or piece.label}]"
        return replace(piece, content=ref, token_estimate=0, tier=TIER_REFERENCE)

    # ── convenience: rank + fit in one call ──────────────────────────
    def allocate_pieces(self, pieces: Iterable[ContextPiece]) -> list[ContextPiece]:
        return self.fit(self.rank(pieces))

    def utilization(self, selected: Iterable[ContextPiece]) -> float:
        used = sum(p.token_estimate for p in selected)
        return used / self.full_budget


def _message_role(item: Any) -> str:
    """Duck-typed role detection — no string splitting, no hard langchain dep."""
    if isinstance(item, dict):
        role = str(item.get("role", "user")).lower()
        return "assistant" if role in ("assistant", "ai") else "user"
    msg_type = getattr(item, "type", None)
    if msg_type in ("ai", "AIMessageChunk"):
        return "assistant"
    if msg_type == "human":
        return "user"
    cls = type(item).__name__
    return "assistant" if cls.startswith("AI") else "user"


def _message_content(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content", ""))
    content = getattr(item, "content", None)
    return str(content) if content is not None else str(item)


class ContextAssembler:
    """Assemble the final LLM message list under budget with cache-friendly layout."""

    def __init__(self, registry: PromptRegistry | None = None,
                 allocator: TokenBudgetAllocator | None = None):
        self.registry = registry or PromptRegistry()
        self.allocator = allocator or TokenBudgetAllocator()
        if not self.registry._versions.get("system"):
            self.registry.register("system", "You are a helpful customer service assistant.")

    # ── piece builders ───────────────────────────────────────────────
    @staticmethod
    def _rag_pieces(rag_results: Any) -> list[ContextPiece]:
        """Build RAG pieces — aligned with the REAL field shapes.

        Agentic shape (what nodes.py actually passes)::

            {"sufficient": bool, "context": str, "rounds": int, "queries_tried": [...]}

        Legacy shape (still accepted)::

            {"title": str, "content": str, "score": float, "relevant": bool}
        """
        if not rag_results:
            return []
        if isinstance(rag_results, dict):
            rag_results = [rag_results]

        pieces: list[ContextPiece] = []
        for idx, r in enumerate(rag_results):
            if not isinstance(r, dict):
                text = str(r)
                if text.strip():
                    pieces.append(ContextPiece("rag", text, 70,
                                               source_id=f"rag:{idx}"))
                continue

            if "context" in r or "sufficient" in r:      # agentic shape
                context = str(r.get("context") or "")
                if not context.strip():
                    continue
                priority = 78 if r.get("sufficient") else 70
                source_id = f"agentic_rag:round{r.get('rounds', 0)}"
                queries = r.get("queries_tried") or []
                header = ""
                if queries:
                    header = f"[retrieval queries: {', '.join(map(str, queries[:4]))}]\n"
                pieces.append(ContextPiece("rag", header + context, priority,
                                           source_id=source_id,
                                           recency=len(rag_results) - idx))
            else:                                        # legacy shape
                title = str(r.get("title", "evidence"))
                source_id = f"doc:{r.get('id', title)}"
                score = float(r.get("score", 0.5) or 0.5)
                if r.get("relevant") is False:
                    # Progressive disclosure: an irrelevant hit must not inject
                    # its full body, but its lightweight metadata/summary can
                    # still help the model understand what was considered and
                    # avoid making the retrieval trace look empty.
                    summary = str(r.get("summary") or r.get("metadata") or "").strip()
                    if not summary:
                        continue
                    pieces.append(ContextPiece(
                        "rag", f"{title}: {summary}", 25,
                        source_id=source_id,
                        recency=len(rag_results) - idx,
                        tier=TIER_REFERENCE,
                        metadata_only=True,
                    ))
                    continue
                content = str(r.get("content", "") or title)
                pieces.append(ContextPiece(
                    "rag", f"{title}: {content}", 70 + int(score * 10),
                    source_id=source_id,
                    recency=len(rag_results) - idx,
                ))
        return pieces

    @staticmethod
    def _tool_pieces(tools: Any) -> list[ContextPiece]:
        if not tools:
            return []
        lines = []
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name", "tool")
                desc = t.get("description", "")
                lines.append(f"- {name}: {desc}".rstrip(": "))
            else:
                lines.append(f"- {t}")
        return [ContextPiece("tools", "Available tools:\n" + "\n".join(lines), 85,
                             source_id="tool_schema")]

    def _history_pieces(self, history: list, current_user: str) -> list[ContextPiece]:
        pieces: list[ContextPiece] = []
        items = list(history or [])
        # De-duplicate: if the newest history entry IS the current user turn,
        # drop it here; the current question is appended (last) separately.
        if items:
            last = items[-1]
            if _message_role(last) == "user" and _message_content(last) == current_user:
                items = items[:-1]

        for index, item in enumerate(items):
            content = _message_content(item)
            if not content.strip():
                continue
            role = _message_role(item)
            emotional = role == "user" and bool(_EMOTIONAL_RE.search(content))
            pieces.append(ContextPiece(
                "history", content,
                priority=50 if emotional else 40,
                role=role,
                source_id=f"history:{index}",
                recency=index,
            ))
        return pieces

    @staticmethod
    def _user_signal_piece(user_message: str, history: list) -> ContextPiece | None:
        """Keep a compact user-signal cue in the system partition.

        The full conversation is still rendered as role messages, but a short
        cue for the current request and the latest emotional user turn keeps
        high-salience intent/emotion available when older history is trimmed.
        This is deliberately limited to emotional turns so ordinary history
        is not duplicated in the system prompt.
        """
        signals: list[str] = []
        current = str(user_message or "").strip()
        if current and _EMOTIONAL_RE.search(current):
            signals.append(f"Current user signal: {current}")

        for item in reversed(list(history or [])):
            if _message_role(item) != "user":
                continue
            content = _message_content(item).strip()
            if content and _EMOTIONAL_RE.search(content):
                if content != current:
                    signals.append(f"Recent user signal: {content}")
                break

        if not signals:
            return None
        return ContextPiece(
            "signals",
            "\n".join(signals),
            priority=88,
            role="system",
            source_id="user_signals",
        )

    def assemble(self, state: dict, user_message: str, session_id: str = "") -> ContextBundle:
        prompt = self.registry.get("system")
        with suppress(Exception):
            self.registry.record_run(prompt, session_id=session_id)

        pieces: list[ContextPiece] = [
            ContextPiece("system", prompt.content, 100, role="system",
                         source_id=f"prompt:{prompt.name}:{prompt.version_no}"),
            ContextPiece("current", user_message or "", 99, role="user",
                         source_id="current_turn"),
        ]

        goal = state.get("task_goal")
        constraints = [c for c in (state.get("constraints") or []) if str(c).strip()]
        if goal or constraints:
            parts = [f"Goal: {goal or ''}".rstrip()]
            if constraints:
                parts.append("Constraints: " + "; ".join(map(str, constraints)))
            pieces.append(ContextPiece("task", "\n".join(parts), 90,
                                       source_id="task_goal"))

        tools = state.get("available_tools") or []
        pieces.extend(self._tool_pieces(tools))
        pieces.extend(self._rag_pieces(state.get("rag_results")))

        memory = state.get("memory_summary") or state.get("memory")
        if memory and str(memory).strip():
            pieces.append(ContextPiece("memory", str(memory), 60,
                                       source_id="memory_summary"))

        history = state.get("messages", [])
        pieces.extend(self._history_pieces(history, user_message))
        signal_piece = self._user_signal_piece(user_message, history)
        if signal_piece is not None:
            pieces.append(signal_piece)

        # rank (importance) and fit (budget + degradation) are decoupled.
        ranked = self.allocator.rank(pieces)
        selected = self.allocator.fit(ranked)

        # ── render: partitions ordered by ascending change frequency ──
        by_label: dict[str, list[ContextPiece]] = {}
        for p in selected:
            by_label.setdefault(p.label, []).append(p)

        system_parts: list[str] = []
        for p in by_label.get("system", []):
            system_parts.append(p.content)
        for p in by_label.get("task", []):
            system_parts.append(f"task_goal:{p.content.strip()}")
        for p in by_label.get("tools", []):
            system_parts.append(p.content)
        for p in by_label.get("signals", []):
            system_parts.append(f"User signals:\n{p.content}")
        rag_selected = by_label.get("rag", [])
        if rag_selected:
            system_parts.append(
                "参考资料 (evidence):\n" + "\n\n".join(p.content for p in rag_selected))
        for p in by_label.get("memory", []):
            system_parts.append(f"Memory Context: {p.content}")

        final_messages: list[dict] = [
            {"role": "system", "content": "\n\n".join(system_parts)}
        ]

        # History in ORIGINAL chronological order (old → new), explicit roles.
        for p in sorted(by_label.get("history", []), key=lambda x: x.recency):
            final_messages.append({"role": p.role, "content": p.content})

        # The current question is ALWAYS the last message.
        current = by_label.get("current")
        final_messages.append({
            "role": "user",
            "content": current[0].content if current else (user_message or ""),
        })

        used_tokens = sum(p.token_estimate for p in selected)
        return ContextBundle(
            messages=final_messages,
            tool_schema=list(tools),
            metadata={
                # ``rag`` means answer-bearing evidence only.  A low-relevance
                # document may still be shown as a metadata/reference stub for
                # transparency, but it must not inflate the hit count.
                "source_counts": {
                    label: (
                        sum(1 for p in items if not p.metadata_only)
                        if label == "rag" else len(items)
                    )
                    for label, items in by_label.items()
                },
                "rag_metadata_count": sum(
                    1 for p in by_label.get("rag", []) if p.metadata_only
                ),
                "token_estimate": used_tokens,
                "utilization": self.allocator.utilization(selected),
                "target_range": (self.allocator.TARGET_LOW, self.allocator.TARGET_HIGH),
                "degraded": [
                    {"source_id": p.source_id, "label": p.label, "tier": p.tier}
                    for p in selected if p.tier != TIER_FULL
                ],
                "prompt_version": f"{prompt.name}:{prompt.version_no}",
                "session_id": session_id,
            },
        )
