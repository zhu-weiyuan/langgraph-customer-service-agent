# -*- coding: utf-8 -*-
"""Unit tests for sentiment analysis module (no LLM required).

Tests the tone adjustment logic and emotion classification rules.
The LLM-based detection is integration-tested separately.
"""

import unittest
from agent.sentiment import get_tone_adjustment


class TestToneAdjustment(unittest.TestCase):
    """Test get_tone_adjustment() output for each emotion/intensity combo."""

    def test_neutral_returns_empty(self):
        """Neutral emotion should return empty string (no adjustment needed)."""
        result = get_tone_adjustment("neutral", 3)
        self.assertEqual(result, "")

    def test_angry_low_intensity(self):
        """Low intensity anger → humble tone."""
        result = get_tone_adjustment("angry", 2)
        self.assertIn("谦逊", result)
        self.assertNotIn("非常愤怒", result)

    def test_angry_medium_intensity(self):
        """Medium intensity anger → sincere apology + patience."""
        result = get_tone_adjustment("angry", 3)
        self.assertIn("道歉", result)
        self.assertIn("理解", result)

    def test_angry_high_intensity(self):
        """High intensity anger → strong apology, no blame-shifting."""
        result = get_tone_adjustment("angry", 5)
        self.assertIn("愤怒", result)
        self.assertIn("道歉", result)
        self.assertIn("推卸责任", result)

    def test_sad_low_intensity(self):
        """Low intensity sadness → warm, encouraging."""
        result = get_tone_adjustment("sad", 2)
        self.assertIn("温和", result)
        self.assertIn("鼓励", result)

    def test_sad_high_intensity(self):
        """High intensity sadness → comfort first."""
        result = get_tone_adjustment("sad", 5)
        self.assertIn("沮丧", result)
        self.assertIn("安慰", result)

    def test_anxious_low_intensity(self):
        """Low intensity anxiety → clear steps."""
        result = get_tone_adjustment("anxious", 2)
        self.assertIn("明确", result)
        self.assertIn("步骤", result)

    def test_anxious_high_intensity(self):
        """High intensity anxiety → immediate clear solution."""
        result = get_tone_adjustment("anxious", 5)
        self.assertIn("焦虑", result)
        self.assertIn("立即", result)

    def test_happy_low_intensity(self):
        """Low intensity happiness → relaxed conversation."""
        result = get_tone_adjustment("happy", 2)
        self.assertIn("轻松", result)

    def test_happy_high_intensity(self):
        """High intensity happiness → lively, enthusiastic."""
        result = get_tone_adjustment("happy", 5)
        self.assertIn("高兴", result)
        self.assertIn("活泼", result)

    def test_unknown_emotion_returns_empty(self):
        """Unknown emotion type should return empty string."""
        result = get_tone_adjustment("confused", 3)
        self.assertEqual(result, "")

    def test_all_adjustments_start_with_note(self):
        """All non-empty adjustments should start with '注意：'."""
        for emotion in ["angry", "sad", "anxious", "happy"]:
            for intensity in range(1, 6):
                result = get_tone_adjustment(emotion, intensity)
                if result:
                    self.assertTrue(result.strip().startswith("注意："),
                                    f"{emotion}/{intensity} should start with '注意：'")


class TestSentimentCache(unittest.TestCase):
    """Test sentiment caching behavior."""

    def setUp(self):
        from agent.sentiment import _sentiment_cache
        _sentiment_cache.clear()

    def tearDown(self):
        from agent.sentiment import _sentiment_cache
        _sentiment_cache.clear()

    def test_cache_clear(self):
        """clear_cache should empty the cache."""
        from agent.sentiment import clear_cache, _sentiment_cache
        _sentiment_cache["test"] = {"emotion": "angry", "intensity": 3}
        self.assertEqual(len(_sentiment_cache), 1)
        clear_cache()
        self.assertEqual(len(_sentiment_cache), 0)


if __name__ == "__main__":
    unittest.main()
