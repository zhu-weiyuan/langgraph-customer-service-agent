from unittest.mock import patch

from agent.llm_client import LLMClient
from agent.sentiment import analyze, clear_cache


def test_extract_json_accepts_emotion_schema():
    parsed = LLMClient._extract_json('{"emotion":"angry","intensity":4}')
    assert parsed == {"emotion": "angry", "intensity": 4}


def test_explicit_anger_is_fast_path():
    clear_cache()
    outcome = analyze("我很生气，你们这是什么处理方式？")
    assert outcome["emotion"] == "angry"
    assert outcome["intensity"] >= 4


def test_contextual_complaint_uses_model_not_keyword_only():
    clear_cache()
    with patch("agent.sentiment._call_llm_json", return_value={"emotion": "angry", "intensity": 4}):
        outcome = analyze("不是哥们，你失忆了吗啊，你给我整这出")
    assert outcome == {"emotion": "angry", "intensity": 4}
