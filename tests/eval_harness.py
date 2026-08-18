"""Reusable rule-based evaluation harness for customer-service graph behavior.

Implements JavaGuide Evaluation Engineering standard:
1. Golden Set: Persistent standard test cases
2. Rule-based Scorers: format/schema/recall/faithfulness
3. LLM-as-Judge Scorer: Auto-evaluate answer quality
4. Eval Runner: Batch evaluation + statistics
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional, List, Dict
from .eval_persistence import EvalResultStore


@dataclass
class GoldenCase:
    """Standard test case for customer service evaluation."""
    id: str
    input: str                          # 用户输入
    expected_output_schema: dict        # 期望的输出结构
    context: Optional[dict] = None      # RAG/memory 上下文
    expected_tools: List[str] = field(default_factory=list)  # 期望调用的工具
    tags: List[str] = field(default_factory=list)  # intent_classification, customer_reply 等
    expected_intent: Optional[str] = None
    expected_reply_keywords: List[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """Evaluation result for a single test case."""
    case_id: str
    actual_output: Any
    scores: Dict[str, float]            # {format: 1.0, recall: 0.8, faithfulness: 0.9}
    judge_reasoning: Optional[str]      # LLM judge 的解释
    passed: bool


class CustomerServiceScorer:
    """Rule-based scorers for customer service evaluation."""
    
    # Backward compatibility aliases for old method names
    def intent_accuracy_scorer(self, expected_intent: str, actual_intent: str) -> int:
        return int(self.score_intent_accuracy(expected_intent, actual_intent))
    
    def satisfaction_response_scorer(self, reply: str, keywords: list = None) -> int:
        return int(self.score_satisfaction_response(reply, keywords) > 0)
    
    def pii_redaction_scorer(self, text: str, pii_patterns: list = None) -> int:
        return int(self.score_pii_redaction(text, pii_patterns))
    
    def response_format_scorer(self, response_json, schema: dict = None) -> int:
        if schema is None:
            schema = {"required": ["intent", "reply"]}
        return int(self.score_response_format(response_json, schema))
    
    def escalation_correctness_scorer(self, reply: str, context: dict) -> int:
        severe = context.get("severity") in {"high", "critical"} or context.get("requires_escalation", False)
        markers = ("escalat", "human", "人工", "升级", "专员")
        did_escalate = any(marker in reply.lower() for marker in markers)
        return int(self.score_escalation_correctness(severe, did_escalate))
    
    def score_intent_accuracy(self, expected_intent: str, actual_intent: str) -> float:
        """Score intent classification accuracy (0 or 1)."""
        return 1.0 if expected_intent == actual_intent else 0.0
    
    def score_satisfaction_response(self, response: str, keywords: List[str] = None) -> float:
        """Score if response contains satisfaction indicators."""
        if not keywords:
            keywords = ["满意", "感谢", "高兴", "抱歉", "理解"]
        response_lower = response.lower()
        matches = sum(1 for kw in keywords if kw.lower() in response_lower)
        return min(1.0, matches / len(keywords)) if keywords else 0.0
    
    def score_pii_redaction(self, output: str, pii_patterns: List[str] = None) -> float:
        """Score PII redaction - 1.0 if no PII detected, 0.0 if PII found."""
        patterns = pii_patterns or [
            r"(?<![\d*])1[3-9]\d{9}(?!\d)",  # Phone numbers
            r"(?<![\d*])\d{17}[\dXx](?!\d)",  # ID cards
            r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}",  # Credit cards
        ]
        has_pii = any(re.search(pattern, output) for pattern in patterns)
        return 0.0 if has_pii else 1.0
    
    def score_response_format(self, output: Any, schema: dict) -> float:
        """Score if output matches expected schema structure."""
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                return 0.0
        
        if not isinstance(output, dict):
            return 0.0
        
        required_keys = schema.get("required", [])
        if not required_keys:
            return 1.0
        
        matches = sum(1 for key in required_keys if key in output)
        return matches / len(required_keys)
    
    def score_escalation_correctness(self, should_escalate: bool, did_escalate: bool) -> float:
        """Score if escalation logic was correctly applied."""
        return 1.0 if should_escalate == did_escalate else 0.0
    
    def score_recall(self, expected_points: List[str], actual_response: str) -> float:
        """Score information recall - how many expected points are covered."""
        if not expected_points:
            return 1.0
        response_lower = actual_response.lower()
        matches = sum(1 for point in expected_points if point.lower() in response_lower)
        return matches / len(expected_points)
    
    def score_faithfulness(self, context: dict, response: str) -> float:
        """Score if response is grounded in provided context (no hallucination)."""
        if not context:
            return 1.0
        
        # Simple heuristic: check if key context terms appear in response
        context_text = " ".join(str(v) for v in context.values() if isinstance(v, str))
        if not context_text:
            return 1.0
        
        context_words = set(context_text.lower().split())
        response_words = set(response.lower().split())
        overlap = context_words & response_words
        
        # Faithfulness = overlap / context size (penalize adding unrelated info)
        return len(overlap) / len(context_words) if context_words else 1.0
    
    def score_tool_usage(self, expected_tools: List[str], actual_tools: List[str]) -> float:
        """Score if expected tools were called."""
        if not expected_tools:
            return 1.0
        expected_set = set(t.lower() for t in expected_tools)
        actual_set = set(t.lower() for t in actual_tools)
        matches = len(expected_set & actual_set)
        return matches / len(expected_set)


class LLMJudgeScorer:
    """Use LLM to evaluate answer quality automatically."""
    
    def __init__(self, judge_model: str = "qwen3.6-35b"):
        self.judge_model = judge_model
        self._client = None
    
    def _get_client(self):
        """Lazy load LLM client."""
        if self._client is None:
            try:
                from agent.llm_gateway import LLMClient
                self._client = LLMClient()
            except ImportError:
                pass
        return self._client
    
    def evaluate(self, question: str, answer: str, criteria: dict) -> dict:
        """
        Use LLM to evaluate answer quality.
        
        Args:
            question: User question
            answer: Model answer to evaluate
            criteria: Evaluation criteria dict with keys like:
                - relevance: Is the answer relevant to the question?
                - accuracy: Is the answer factually accurate?
                - completeness: Does the answer cover all aspects?
                - clarity: Is the answer clear and well-structured?
        
        Returns:
            dict with {score: 0-5, reasoning: "..."}
        """
        client = self._get_client()
        
        criteria_text = "\n".join(f"- {k}: {v}" for k, v in criteria.items())
        
        prompt = f"""You are an expert evaluator. Evaluate the following answer based on the criteria.

Question: {question}

Answer: {answer}

Evaluation Criteria:
{criteria_text}

Rate the answer from 0-5 (0=worst, 5=best) and provide brief reasoning.

Format your response as JSON:
{{
    "score": <number 0-5>,
    "reasoning": "<brief explanation>"
}}

Respond with ONLY the JSON object."""

        try:
            if client:
                response = client.generate(prompt, model=self.judge_model)
                result = json.loads(response)
                return {
                    "score": float(result.get("score", 0)),
                    "reasoning": result.get("reasoning", "")
                }
        except Exception:
            pass
        
        # Fallback: simple heuristic scoring
        score = 3.0  # Default neutral score
        reasoning = "Fallback heuristic evaluation"
        
        if len(answer) < 10:
            score = 1.0
            reasoning = "Answer too short"
        elif "?" in question and any(kw in answer.lower() for kw in ["answer", "response", "reply"]):
            score = 4.0
            reasoning = "Answer appears responsive"
        
        return {"score": score, "reasoning": reasoning}


class EvalRunner:
    """Run evaluation suite and generate statistics."""
    
    def __init__(self, evaluator: CustomerServiceScorer = None, 
                 llm_judge: LLMJudgeScorer = None,
                 store: EvalResultStore = None,
                 task_name: str = "customer_service",
                 export_path: str = None):
        self.evaluator = evaluator or CustomerServiceScorer()
        self.llm_judge = llm_judge or LLMJudgeScorer()
        self.store = store or EvalResultStore()
        self.task_name = task_name
        self.export_path = Path(export_path) if export_path else Path(__file__).with_name("eval_results.json")
    
    def load_dataset(self, path: str | Path) -> list[dict[str, Any]]:
        """Load the lightweight dictionary dataset used by the phase-C demo.

        ``load_golden_set`` is the typed API used by the main harness.  Older
        evaluation scripts used ``load_dataset`` and then edited dictionaries
        in-place, so keep that API as an explicit compatibility layer instead
        of making callers depend on the internal ``GoldenCase`` dataclass.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("evaluation dataset must be a JSON array")
        return [dict(item) for item in data if isinstance(item, dict)]

    def load_golden_set(self, path: str | Path) -> List[GoldenCase]:
        """Load golden set from JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        cases = []
        for item in data:
            case = GoldenCase(
                id=item["id"],
                input=item["input"],
                expected_output_schema=item.get("expected_output_schema", {}),
                context=item.get("context"),
                expected_tools=item.get("expected_tools", []),
                tags=item.get("tags", []),
                expected_intent=item.get("expected_intent"),
                expected_reply_keywords=item.get("expected_reply_keywords", [])
            )
            cases.append(case)
        return cases
    
    def run(self, dataset: list[dict[str, Any]], graph=None) -> Dict[str, Any]:
        """Compatibility runner for the original dictionary-based dataset.

        The phase-C smoke test supplies ``actual`` outputs directly.  Convert
        those records to typed cases and reuse the same persistence/export
        path as the full harness.
        """
        cases: list[GoldenCase] = []
        for item in dataset or []:
            # The legacy dataset's ``context`` contains routing hints such as
            # severity/escalation flags, not factual answer evidence.  Feeding
            # those flags into the faithfulness scorer would incorrectly mark
            # an otherwise correct smoke-test reply as hallucinated.
            raw_context = dict(item.get("context") or {})
            context = {}
            for key in ("expected_points", "actual"):
                if key in raw_context:
                    context[key] = raw_context[key]
            if "actual" in item:
                context["actual"] = item["actual"]
            cases.append(GoldenCase(
                id=str(item.get("id", len(cases))),
                input=item.get("input", ""),
                expected_output_schema=item.get("expected_output_schema", {}),
                context=context,
                expected_tools=item.get("expected_tools", []),
                tags=item.get("tags", []),
                expected_intent=item.get("expected_intent"),
                expected_reply_keywords=item.get("expected_reply_keywords", []),
            ))
        return self.run_suite(cases, graph=graph)

    def run_suite(self, golden_set: List[GoldenCase], graph=None) -> Dict[str, Any]:
        """
        Run evaluation suite on all golden cases.
        
        Args:
            golden_set: List of GoldenCase objects
            graph: Optional graph to invoke for actual outputs
        
        Returns:
            Dict with summary statistics and detailed results
        """
        results: List[EvalResult] = []
        
        for case in golden_set:
            # Get actual output
            if graph:
                actual_output = graph.invoke(case.input)
            else:
                actual_output = case.context.get("actual", {}) if case.context else {}
            
            # Calculate scores
            scores = {}
            
            # Format score
            if case.expected_output_schema:
                scores["format"] = self.evaluator.score_response_format(
                    actual_output, case.expected_output_schema
                )
            
            # Intent score
            if case.expected_intent:
                actual_intent = actual_output.get("intent", "") if isinstance(actual_output, dict) else ""
                scores["intent"] = self.evaluator.score_intent_accuracy(
                    case.expected_intent, actual_intent
                )
            
            # PII score
            reply = actual_output.get("reply", "") if isinstance(actual_output, dict) else str(actual_output)
            scores["pii"] = self.evaluator.score_pii_redaction(reply)
            
            # Recall score
            if case.context and "expected_points" in case.context:
                scores["recall"] = self.evaluator.score_recall(
                    case.context["expected_points"], reply
                )
            
            # Faithfulness score
            if case.context:
                scores["faithfulness"] = self.evaluator.score_faithfulness(
                    case.context, reply
                )
            
            # Tool usage score
            if case.expected_tools:
                actual_tools = actual_output.get("tools_used", []) if isinstance(actual_output, dict) else []
                scores["tools"] = self.evaluator.score_tool_usage(
                    case.expected_tools, actual_tools
                )
            
            # LLM judge evaluation (optional, for complex cases)
            judge_reasoning = None
            if "llm_judge" in case.tags:
                judge_result = self.llm_judge.evaluate(
                    case.input,
                    reply,
                    criteria={"relevance": "Is answer relevant?", "accuracy": "Is answer accurate?"}
                )
                scores["llm_judge"] = judge_result["score"] / 5.0  # Normalize to 0-1
                judge_reasoning = judge_result["reasoning"]
            
            # Determine if passed (all scores >= 0.8)
            passed = all(score >= 0.8 for score in scores.values()) if scores else False
            
            result = EvalResult(
                case_id=case.id,
                actual_output=actual_output,
                scores=scores,
                judge_reasoning=judge_reasoning,
                passed=passed
            )
            results.append(result)
            
            # Record to store
            self.store.record_result(case.id, self.task_name, scores, passed)
        
        # Export results
        self.store.export_to_json(self.export_path)
        
        # Calculate summary statistics
        total = len(results)
        passed_count = sum(1 for r in results if r.passed)
        avg_scores = {}
        if results:
            for key in results[0].scores.keys():
                avg_scores[key] = sum(r.scores.get(key, 0) for r in results) / total
        
        return {
            "total": total,
            "passed": passed_count,
            "pass_rate": passed_count / total if total else 0,
            "average_scores": avg_scores,
            "failed_cases": [r.case_id for r in results if not r.passed],
            "results": [asdict(r) for r in results]
        }


# Backward compatibility aliases
CustomerServiceEvaluator = CustomerServiceScorer
EvaluationRunner = EvalRunner
