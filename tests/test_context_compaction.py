#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Context Compaction 单元测试

测试场景：
1. 短对话不触发压缩
2. 长对话触发压缩
3. 强制压缩
4. Token 阈值触发
5. 缓存命中
6. Fallback summary（LLM 不可用）
7. 结构化摘要格式化
"""

import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import HumanMessage, AIMessage

from agent.context_compaction import (
    ContextCompactor,
    CompactionResult,
    _estimate_tokens,
    _estimate_messages_tokens,
    COMPACTION_TRIGGER_MESSAGES,
    KEEP_RECENT_TURNS,
)


# ── 辅助函数 ──────────────────────────────────────────

def make_messages(n_pairs: int) -> list:
    """生成 n_pairs 轮对话（user + assistant）。"""
    msgs = []
    for i in range(n_pairs):
        msgs.append(HumanMessage(content=f"用户第{i+1}条消息，这是测试内容"))
        msgs.append(AIMessage(content=f"客服第{i+1}条回复，这是测试回复内容"))
    return msgs


# ── Token 估算测试 ────────────────────────────────────

class TestTokenEstimation:
    def test_chinese_text(self):
        tokens = _estimate_tokens("你好世界")
        assert 4 <= tokens <= 8  # ~1.5 per char

    def test_english_text(self):
        tokens = _estimate_tokens("hello world")
        assert 2 <= tokens <= 10

    def test_empty(self):
        assert _estimate_tokens("") == 0

    def test_messages_tokens(self):
        msgs = make_messages(3)
        total = _estimate_messages_tokens(msgs)
        assert total > 0


# ── 压缩逻辑测试 ──────────────────────────────────────

class TestCompactionLogic:
    def test_short_dialogue_no_compact(self):
        """短对话不触发压缩。"""
        compactor = ContextCompactor()
        msgs = make_messages(3)  # 6 messages < threshold
        result = compactor.maybe_compact(msgs, session_id="test")

        assert result.compacted is False
        assert result.summary == ""
        assert len(result.messages) == len(msgs)

    def test_long_dialogue_triggers_compact(self):
        """超过阈值时触发压缩。"""
        compactor = ContextCompactor()
        msgs = make_messages(10)  # 20 messages > threshold=16

        with patch.object(compactor, '_compact_old_messages') as mock_compact:
            mock_compact.return_value = "【对话历史摘要】测试摘要"
            result = compactor.maybe_compact(msgs, session_id="test")

        assert result.compacted is True
        assert result.summary == "【对话历史摘要】测试摘要"
        # 保留最近 KEEP_RECENT_TURNS*2 = 10 条
        assert len(result.messages) == 1 + KEEP_RECENT_TURNS * 2
        assert result.tokens_saved > 0

    def test_force_compaction(self):
        """强制压缩忽略阈值。"""
        compactor = ContextCompactor()
        msgs = make_messages(7)  # 14 messages > keep(10), force=True

        with patch.object(compactor, '_compact_old_messages') as mock_compact:
            mock_compact.return_value = "【对话历史摘要】强制压缩"
            result = compactor.maybe_compact(msgs, session_id="test", force=True)

        assert result.compacted is True

    def test_token_threshold(self):
        """Token 超过阈值也触发。"""
        compactor = ContextCompactor()
        # 生成足够多的长消息（> keep=10 条）
        msgs = []
        for i in range(6):
            msgs.append(HumanMessage(content="A" * 20000))
            msgs.append(AIMessage(content="B" * 20000))

        with patch.object(compactor, '_compact_old_messages') as mock_compact:
            mock_compact.return_value = "【对话历史摘要】Token阈值触发"
            result = compactor.maybe_compact(msgs, session_id="test")

        assert result.compacted is True

    def test_cache_hit(self):
        """缓存命中时不重新压缩。"""
        compactor = ContextCompactor()
        msgs = make_messages(10)

        # 预先写入缓存
        compactor._summary_cache["cached_session"] = "【对话历史摘要】缓存摘要"

        result = compactor.maybe_compact(msgs, session_id="cached_session")

        assert result.compacted is False  # 来自缓存，不算新压缩
        assert result.summary == "【对话历史摘要】缓存摘要"


# ── 摘要格式化测试 ────────────────────────────────────

class TestSummaryFormatting:
    def test_full_structured_summary(self):
        compactor = ContextCompactor()
        data = {
            "products_mentioned": ["智能音箱", "云服务"],
            "resolved_issues": ["退货问题已解决"],
            "pending_issues": ["发票未开具"],
            "user_preferences": {"饮食": "素食"},
            "emotion_summary": "用户从不满转为满意",
            "key_facts": ["订单号12345", "购买日期2026-06-01"],
        }

        summary = compactor._format_summary(data)

        assert "智能音箱" in summary
        assert "云服务" in summary
        assert "退货问题已解决" in summary
        assert "发票未开具" in summary
        assert "素食" in summary
        assert "订单号12345" in summary

    def test_empty_summary(self):
        compactor = ContextCompactor()
        summary = compactor._format_summary({})
        assert "对话历史摘要" in summary

    def test_partial_data(self):
        compactor = ContextCompactor()
        data = {"key_facts": ["只有这个"]}
        summary = compactor._format_summary(data)
        assert "只有这个" in summary
        assert "已解决" not in summary


# ── Fallback 测试 ────────────────────────────────────

class TestFallback:
    def test_fallback_with_user_messages(self):
        compactor = ContextCompactor()
        msgs = [
            HumanMessage(content="我想退货"),
            AIMessage(content="好的，请提供订单号"),
            HumanMessage(content="订单号12345"),
            AIMessage(content="已查到您的订单"),
        ]

        summary = compactor._fallback_summary(msgs)

        assert "对话历史摘要" in summary
        # 应该包含用户消息的片段
        assert "退货" in summary or "订单" in summary

    def test_fallback_empty(self):
        compactor = ContextCompactor()
        msgs = [AIMessage(content="只有AI消息")]
        summary = compactor._fallback_summary(msgs)
        assert "压缩失败" in summary


# ── LLM 调用测试（mock） ─────────────────────────────

class TestLLMIntegration:
    def test_compact_with_valid_json(self):
        """LLM 返回合法 JSON，走 Gateway。"""
        compactor = ContextCompactor()
        msgs = make_messages(5)

        mock_response = json.dumps({
            "products_mentioned": ["音箱"],
            "resolved_issues": [],
            "pending_issues": ["发票问题"],
            "user_preferences": {},
            "emotion_summary": "",
            "key_facts": ["订单号999"],
        })

        mock_gw = MagicMock()
        mock_gw.chat_sync.return_value = MagicMock(content=mock_response)

        # Patch the lazy loader to return a mock gateway module
        from agent import context_compaction as cc_module
        original_getter = cc_module._get_llm_gateway_module

        def fake_getter():
            class FakeGW:
                @staticmethod
                def get_llm_gateway():
                    return mock_gw
            return FakeGW()

        cc_module._get_llm_gateway_module = fake_getter
        try:
            summary = compactor._compact_old_messages(msgs)
        finally:
            cc_module._get_llm_gateway_module = original_getter

        assert "音箱" in summary or "订单号999" in summary

    def test_compact_with_raw_text(self):
        """Gateway 失败 → 直接 LLM 返回非 JSON。"""
        compactor = ContextCompactor()
        msgs = make_messages(5)

        raw_text = "用户咨询了智能音箱的退货问题，订单号12345，已解决。"

        from agent import context_compaction as cc_module
        original_getter = cc_module._get_llm_gateway_module

        def fake_getter():
            raise RuntimeError("gateway down")

        cc_module._get_llm_gateway_module = fake_getter
        try:
            with patch.object(compactor, '_get_llm') as mock_llm:
                mock_llm.return_value.chat.return_value = raw_text
                summary = compactor._compact_old_messages(msgs)
        finally:
            cc_module._get_llm_gateway_module = original_getter

        assert len(summary) > 0

    def test_compact_llm_fails(self):
        """Gateway + LLM 都不可用，走 fallback。"""
        compactor = ContextCompactor()
        msgs = make_messages(5)

        from agent import context_compaction as cc_module
        original_getter = cc_module._get_llm_gateway_module

        def fake_getter():
            raise RuntimeError("gateway down")

        cc_module._get_llm_gateway_module = fake_getter
        try:
            with patch.object(compactor, '_get_llm') as mock_llm:
                mock_llm.return_value.chat.side_effect = Exception("LLM error")
                summary = compactor._compact_old_messages(msgs)
        finally:
            cc_module._get_llm_gateway_module = original_getter

        assert "压缩失败" in summary or "对话历史摘要" in summary


# ── 统计测试 ──────────────────────────────────────────

class TestStats:
    def test_cache_stats(self):
        compactor = ContextCompactor()
        compactor._summary_cache["s1"] = "摘要1"
        compactor._summary_cache["s2"] = "摘要2"

        stats = compactor.get_cache_stats()
        assert stats["cached_sessions"] == 2


# ── Import json for tests ────────────────────────────
import json

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
