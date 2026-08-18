# -*- coding: utf-8 -*-
"""Unit tests for dialogue summary module (no LLM required).

Tests ticket formatting and structure. The LLM-based summarization
is integration-tested separately.
"""

import unittest
from agent.summary import format_ticket


class TestFormatTicket(unittest.TestCase):
    """Test ticket formatting output."""

    def setUp(self):
        self.sample_ticket = {
            "ticket_id": "T-20260503-0300",
            "issue_category": "技术支持",
            "description": "音箱无法连接WiFi",
            "resolution": "指导用户重置设备并重新配网",
            "satisfaction": "满意",
            "priority": "medium",
            "emotion": "anxious",
            "emotion_intensity": 3,
            "message_count": 8,
            "created_at": "2026-05-03T03:00:00",
        }

    def test_format_contains_ticket_id(self):
        """Formatted output should contain ticket ID."""
        output = format_ticket(self.sample_ticket)
        self.assertIn("T-20260503-0300", output)

    def test_format_contains_category(self):
        """Formatted output should contain issue category."""
        output = format_ticket(self.sample_ticket)
        self.assertIn("技术支持", output)

    def test_format_contains_priority_emoji(self):
        """Priority should have emoji indicator."""
        output = format_ticket(self.sample_ticket)
        self.assertIn("🟡 中", output)

    def test_high_priority_emoji(self):
        """High priority should show red circle."""
        ticket = dict(self.sample_ticket)
        ticket["priority"] = "high"
        output = format_ticket(ticket)
        self.assertIn("🔴 高", output)

    def test_low_priority_emoji(self):
        """Low priority should show green circle."""
        ticket = dict(self.sample_ticket)
        ticket["priority"] = "low"
        self.assertIn("🟢 低", format_ticket(ticket))

    def test_format_contains_description(self):
        """Formatted output should contain description."""
        output = format_ticket(self.sample_ticket)
        self.assertIn("音箱无法连接WiFi", output)

    def test_format_contains_resolution(self):
        """Formatted output should contain resolution."""
        output = format_ticket(self.sample_ticket)
        self.assertIn("指导用户重置设备", output)

    def test_format_multiline(self):
        """Output should be multi-line formatted text."""
        output = format_ticket(self.sample_ticket)
        lines = output.split("\n")
        self.assertGreaterEqual(len(lines), 5)

    def test_unknown_priority_fallback(self):
        """Unknown priority should show raw value."""
        ticket = dict(self.sample_ticket)
        ticket["priority"] = "unknown"
        output = format_ticket(ticket)
        self.assertIn("unknown", output)


class TestTicketStructure(unittest.TestCase):
    """Test expected ticket field structure."""

    def test_required_fields(self):
        """A valid ticket should have all required fields."""
        required_fields = [
            "ticket_id", "issue_category", "description",
            "resolution", "satisfaction", "priority",
            "created_at"
        ]
        sample = {
            "ticket_id": "T-001",
            "issue_category": "咨询",
            "description": "test",
            "resolution": "resolved",
            "satisfaction": "满意",
            "priority": "low",
            "created_at": "2026-05-03T00:00:00",
        }
        for field in required_fields:
            self.assertIn(field, sample)


if __name__ == "__main__":
    unittest.main()
