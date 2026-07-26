# -*- coding: utf-8 -*-
"""Unit tests for message trimming (no LLM required).

Tests the _trim_messages helper that prevents unbounded checkpoint growth.
Based on LangGraph best practice: manage short-term memory by filtering stale messages.
Ref: docs.langchain.com/oss/python/langgraph/add-memory
"""

import unittest
from langchain_core.messages import HumanMessage, AIMessage
from agent.nodes import _trim_messages


class TestTrimMessages(unittest.TestCase):
    """Test _trim_messages() preserves recency while limiting state size."""

    def _make_messages(self, n: int):
        """Create n alternating user/assistant messages."""
        msgs = []
        for i in range(n):
            if i % 2 == 0:
                msgs.append(HumanMessage(content=f"user message {i}"))
            else:
                msgs.append(AIMessage(content=f"bot reply {i}"))
        return msgs

    def test_short_list_unchanged(self):
        """Lists within limit are returned as-is."""
        msgs = self._make_messages(4)
        result = _trim_messages(msgs, keep_last=10)
        self.assertEqual(len(result), 4)
        self.assertIs(result, msgs)  # same object (no copy needed)

    def test_exact_limit_unchanged(self):
        """Exactly at limit: no trimming."""
        msgs = self._make_messages(20)  # keep_last=10 * 2 = 20
        result = _trim_messages(msgs, keep_last=10)
        self.assertEqual(len(result), 20)
        self.assertIs(result, msgs)

    def test_over_limit_trims(self):
        """Over limit: keeps only the last N pairs."""
        msgs = self._make_messages(30)
        result = _trim_messages(msgs, keep_last=5)
        self.assertEqual(len(result), 10)  # 5 pairs * 2
        # First kept message should be index 20
        self.assertIn("message 20", result[0].content)

    def test_preserves_order(self):
        """Trimmed messages maintain original order."""
        msgs = self._make_messages(20)
        result = _trim_messages(msgs, keep_last=3)
        contents = [m.content for m in result]
        self.assertEqual(contents[0], "user message 14")
        self.assertEqual(contents[-1], "bot reply 19")

    def test_empty_list(self):
        """Empty list returns empty."""
        result = _trim_messages([], keep_last=5)
        self.assertEqual(result, [])

    def test_single_message(self):
        """Single message preserved."""
        msgs = [HumanMessage(content="hello")]
        result = _trim_messages(msgs, keep_last=5)
        self.assertEqual(len(result), 1)

    def test_odd_count(self):
        """Odd number of messages handled correctly."""
        msgs = self._make_messages(11)  # 6 user + 5 assistant
        result = _trim_messages(msgs, keep_last=2)
        self.assertEqual(len(result), 4)  # last 4 messages


if __name__ == "__main__":
    unittest.main()
