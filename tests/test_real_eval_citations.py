# -*- coding: utf-8 -*-
"""Pure regression tests for citation-integrity accounting in the real evaluator."""
import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "eval" / "run_real_eval.py"
_SPEC = importlib.util.spec_from_file_location("real_eval_citation_test_module", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class TestCitationIntegrity:
    def test_source_level_judge_response_handles_repeated_marker_compatibly(self):
        result = _MODULE.RealEvaluator._citation_integrity(
            answer_markers=[1, 1, 2, 1],
            citation_details=[
                {"n": 1, "supported": True},
                {"n": 2, "supported": True},
            ],
            n_contexts=3,
        )
        assert result["citation_valid"] is True
        assert result["citation_detail_mode"] == "per_source_compatible"

    def test_occurrence_aware_judge_response_requires_each_position_and_number(self):
        result = _MODULE.RealEvaluator._citation_integrity(
            answer_markers=[1, 1, 2],
            citation_details=[
                {"occurrence": 1, "n": 1, "supported": True},
                {"occurrence": 2, "n": 1, "supported": True},
                {"occurrence": 3, "n": 2, "supported": False},
            ],
            n_contexts=3,
        )
        assert result["citation_valid"] is True
        assert result["citation_detail_mode"] == "per_occurrence"

    def test_occurrence_aware_judge_response_flags_missing_or_wrong_positions(self):
        result = _MODULE.RealEvaluator._citation_integrity(
            answer_markers=[1, 2],
            citation_details=[
                {"occurrence": 1, "n": 2, "supported": True},
            ],
            n_contexts=3,
        )
        assert result["citation_valid"] is False
        assert result["citation_detail_mode"] == "per_occurrence"
        assert result["citation_errors"]


class TestCitationAccuracy:
    def test_missing_occurrence_is_not_removed_from_denominator(self):
        score = _MODULE.RealEvaluator._citation_accuracy_from_details(
            answer_markers=[1, 1, 2],
            citation_details=[
                {"occurrence": 1, "n": 1, "supported": True},
                {"occurrence": 2, "n": 1, "supported": True},
            ],
            n_contexts=3,
        )
        assert score == 2 / 3

    def test_legacy_source_level_details_are_weighted_by_marker_occurrences(self):
        score = _MODULE.RealEvaluator._citation_accuracy_from_details(
            answer_markers=[1, 1, 2],
            citation_details=[
                {"n": 1, "supported": True},
                {"n": 2, "supported": False},
            ],
            n_contexts=3,
        )
        assert score == 2 / 3


class TestJudgeJsonExtraction:
    def test_extract_json_skips_unrelated_object_using_metric_schema(self):
        raw = '示例：{"score": 1}\n最终：{"citations": [], "citation_accuracy": 0.0}'
        assert _MODULE.extract_json(
            raw, required_keys=("citations", "citation_accuracy")
        ) == {"citations": [], "citation_accuracy": 0.0}

    def test_metric_schema_selects_later_judge_object(self):
        assert _MODULE._required_judge_keys("context_precision[2]") == ("relevant",)
        raw = '无关：{"score": 5} 最终：{"relevant": true, "reason": "相关"}'
        assert _MODULE.extract_json(
            raw, required_keys=_MODULE._required_judge_keys("context_precision[2]")
        )["relevant"] is True
