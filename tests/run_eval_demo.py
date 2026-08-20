#!/usr/bin/env python
"""Demo script to run the evaluation harness with the golden set."""
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.eval_harness import EvalRunner, GoldenCase
from tests.eval_persistence import EvalResultStore


def main():
    """Run evaluation demo with sample golden set."""
    print("=" * 60)
    print("Customer Service Agent - Evaluation Harness Demo")
    print("=" * 60)
    
    # Setup paths
    base_path = Path(__file__).parent
    golden_set_path = base_path / "data" / "sample_golden_set.json"
    db_path = base_path / "eval_results.db"
    export_path = base_path / "eval_results_demo.json"
    
    print(f"\n[DATA] Golden Set: {golden_set_path}")
    print(f"[DATA] Database: {db_path}")
    print(f"[DATA] Export: {export_path}")
    
    # Create runner
    store = EvalResultStore(db_path)
    runner = EvalRunner(
        task_name="customer_service_demo",
        store=store,
        export_path=export_path
    )
    
    # Load golden set
    print(f"\n[LOAD] Loading golden set...")
    cases = runner.load_golden_set(golden_set_path)
    print(f"   Loaded {len(cases)} test cases")
    
    # Show case distribution
    tags_count = {}
    for case in cases:
        for tag in case.tags:
            tags_count[tag] = tags_count.get(tag, 0) + 1
    
    print(f"\n[TAGS] Test case distribution:")
    for tag, count in sorted(tags_count.items()):
        print(f"   - {tag}: {count}")
    
    # Mock actual outputs for demo (in real usage, this would invoke the graph)
    print(f"\n[RUN] Running evaluation (mock mode)...")
    for case in cases:
        if case.context is None:
            case.context = {}
        
        # Generate mock output based on expected intent
        mock_output = {
            "intent": case.expected_intent or "consult",
            "reply": f"您好，关于{case.input}的问题，我会帮您处理。",
        }
        
        # Add escalation for high severity cases
        if case.context and case.context.get("severity") in ["high", "critical"]:
            mock_output["escalation"] = True
            mock_output["reply"] += " 已为您升级人工专员处理。"
        
        case.context["actual"] = mock_output
    
    # Run evaluation
    results = runner.run_suite(cases)
    
    # Print summary
    print("\n" + "=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)
    print(f"   Total cases:     {results['total']}")
    print(f"   Passed:          {results['passed']}")
    print(f"   Failed:          {results['total'] - results['passed']}")
    print(f"   Pass rate:       {results['pass_rate']:.1%}")
    
    print(f"\n[SCORES] Average scores by metric:")
    for metric, score in sorted(results['average_scores'].items()):
        bar = "#" * int(score * 10)
        print(f"   {metric:15} {score:.2f} {bar}")
    
    if results['failed_cases']:
        print(f"\n[FAIL] Failed cases:")
        for case_id in results['failed_cases']:
            print(f"   - {case_id}")
    
    print(f"\n[EXPORT] Results exported to: {export_path}")
    print(f"[EXPORT] Database: {db_path}")
    
    # Show sample results
    print(f"\n[SAMPLE] Sample evaluation results:")
    sample_results = results['results'][:3]
    for result in sample_results:
        print(f"\n   Case: {result['case_id']}")
        print(f"   Passed: {result['passed']}")
        print(f"   Scores: {json.dumps(result['scores'], indent=6, ensure_ascii=False)}")
        if result.get('judge_reasoning'):
            print(f"   Judge: {result['judge_reasoning'][:80]}...")
    
    print("\n" + "=" * 60)
    print("Demo completed successfully!")
    print("=" * 60)
    
    return results


if __name__ == "__main__":
    main()
