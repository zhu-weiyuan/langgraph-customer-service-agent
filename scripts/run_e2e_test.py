# -*- coding: utf-8 -*-
"""
E2E 测试运行脚本 - 用户名 zwy
使用 golden_set_500_zwy.json 进行完整端到端测试
"""
import json
import sys
import time
from datetime import datetime
from typing import Dict, List, Any
sys.stdout.reconfigure(encoding='utf-8')

# Add parent directory to path
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import _build_core_graph

def load_golden_set(path: str) -> List[Dict]:
    """加载 golden set 测试数据"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def run_single_test(graph, test_case: Dict, session_id: str) -> Dict[str, Any]:
    """运行单个测试用例"""
    input_text = test_case['input']
    expected_intent = test_case['expected_intent']
    expected_keywords = test_case.get('expected_reply_keywords', [])
    
    config = {"configurable": {"thread_id": session_id}}
    
    input_data = {
        "messages": [HumanMessage(content=input_text)],
        "retry_count": 0,
        "escalate": False
    }
    
    start_time = time.time()
    try:
        # Run graph with config
        result = graph.invoke(input_data, config=config)
        elapsed = time.time() - start_time
        
        if result:
            messages = result.get('messages', [])
            bot_reply = result.get('bot_reply', '')
            
            # Get last AI message
            last_ai_msg = None
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    last_ai_msg = msg.content
                    break
            
            reply_text = last_ai_msg or bot_reply or ""
            actual_intent = result.get('intent', 'unknown')
            
            # Check results
            intent_match = actual_intent == expected_intent
            
            keyword_match = True
            missed_keywords = []
            for kw in expected_keywords:
                if kw not in reply_text:
                    keyword_match = False
                    missed_keywords.append(kw)
            
            return {
                'success': True,
                'elapsed': elapsed,
                'actual_intent': actual_intent,
                'intent_match': intent_match,
                'keyword_match': keyword_match,
                'missed_keywords': missed_keywords,
                'reply_length': len(reply_text),
                'error': None
            }
        else:
            return {
                'success': False,
                'elapsed': time.time() - start_time,
                'error': 'No result returned'
            }
            
    except Exception as e:
        return {
            'success': False,
            'elapsed': time.time() - start_time,
            'error': str(e)
        }

def run_e2e_tests(golden_set_path: str, user_name: str = "zwy", max_tests: int = None):
    """运行完整的 E2E 测试"""
    print("=" * 70)
    print(f"E2E 测试启动 - 用户：{user_name}")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Load test data
    print("\n[1/5] 加载测试数据...")
    golden_set = load_golden_set(golden_set_path)
    if max_tests:
        golden_set = golden_set[:max_tests]
    print(f"  总计：{len(golden_set)} 个测试用例")
    
    # Category breakdown
    categories = {}
    for case in golden_set:
        cat = case.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
    print("  分类:")
    for cat, count in sorted(categories.items()):
        print(f"    - {cat}: {count}")
    
    # Build graph
    print("\n[2/5] 初始化 LangGraph...")
    try:
        # Build graph with memory checkpointer for E2E testing
        from agent.graph import _build_core_graph
        from langgraph.checkpoint.memory import MemorySaver
        
        checkpointer = MemorySaver()
        graph = _build_core_graph()
        graph = graph.compile(checkpointer=checkpointer)
        print("  Graph 初始化完成 (MemorySaver 模式)")
    except Exception as e:
        print(f"  Graph 初始化失败：{e}")
        raise
    
    # Run tests
    print("\n[3/5] 执行 E2E 测试...")
    results = {
        'total': 0,
        'passed': 0,
        'failed': 0,
        'errors': 0,
        'by_category': {},
        'details': []
    }
    
    for i, test_case in enumerate(golden_set, 1):
        test_id = test_case['id']
        category = test_case.get('category', 'unknown')
        input_preview = test_case['input'][:50] + "..." if len(test_case['input']) > 50 else test_case['input']
        
        # Run test
        session_id = f"e2e_{user_name}_{i}"
        result = run_single_test(graph, test_case, session_id)
        result['test_id'] = test_id
        result['category'] = category
        result['input'] = test_case['input']
        
        # Update stats
        results['total'] += 1
        if category not in results['by_category']:
            results['by_category'][category] = {'total': 0, 'passed': 0, 'failed': 0}
        results['by_category'][category]['total'] += 1
        
        if result['success']:
            if result.get('intent_match') and result.get('keyword_match'):
                results['passed'] += 1
                results['by_category'][category]['passed'] += 1
                status = "PASS"
            else:
                results['failed'] += 1
                results['by_category'][category]['failed'] += 1
                status = "FAIL"
        else:
            results['errors'] += 1
            results['by_category'][category]['failed'] += 1
            status = "ERROR"
        
        result['status'] = status
        results['details'].append(result)
        
        # Progress output (every 50 tests)
        if i % 50 == 0 or i == len(golden_set):
            current_pass_rate = results['passed'] / results['total'] * 100
            print(f"  [{i}/{results['total']}] 通过率：{current_pass_rate:.1f}% | 最新：{status} ({test_id})")
    
    # Generate report
    print("\n[4/5] 生成测试报告...")
    overall_pass_rate = results['passed'] / results['total'] * 100 if results['total'] > 0 else 0
    
    print("\n" + "=" * 70)
    print("E2E 测试结果汇总")
    print("=" * 70)
    print(f"总用例数：{results['total']}")
    print(f"通过：{results['passed']} ({results['passed']/results['total']*100:.1f}%)")
    print(f"失败：{results['failed']} ({results['failed']/results['total']*100:.1f}%)")
    print(f"错误：{results['errors']} ({results['errors']/results['total']*100:.1f}%)")
    print(f"总体通过率：{overall_pass_rate:.1f}%")
    
    print("\n按分类统计:")
    print("-" * 70)
    print(f"{'类别':<20} {'总数':>6} {'通过':>6} {'失败':>6} {'通过率':>10}")
    print("-" * 70)
    for cat, stats in sorted(results['by_category'].items()):
        cat_pass_rate = stats['passed'] / stats['total'] * 100 if stats['total'] > 0 else 0
        print(f"{cat:<20} {stats['total']:>6} {stats['passed']:>6} {stats['total']-stats['passed']:>6} {cat_pass_rate:>9.1f}%")
    print("-" * 70)
    
    # Save detailed results
    print("\n[5/5] 保存详细结果...")
    output_file = f"e2e_results_{user_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  详细结果已保存到：{output_file}")
    
    # Print failure analysis
    if results['failed'] > 0 or results['errors'] > 0:
        print("\n失败案例分析 (前 10 个):")
        print("-" * 70)
        failed_cases = [r for r in results['details'] if r['status'] in ['FAIL', 'ERROR']]
        for case in failed_cases[:10]:
            print(f"\n  ID: {case['test_id']}")
            print(f"  分类：{case['category']}")
            print(f"  输入：{case['input'][:100]}...")
            if case.get('error'):
                print(f"  错误：{case['error']}")
            else:
                if not case.get('intent_match'):
                    print(f"  意图不匹配：期望={case.get('expected_intent')}, 实际={case.get('actual_intent')}")
                if not case.get('keyword_match'):
                    print(f"  缺失关键词：{case.get('missed_keywords')}")
    
    print("\n" + "=" * 70)
    print(f"E2E 测试完成 - 用户：{user_name}")
    print("=" * 70)
    
    return results

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='运行 E2E 测试')
    parser.add_argument('--user', default='zwy', help='用户名')
    parser.add_argument('--max-tests', type=int, default=None, help='最大测试用例数')
    parser.add_argument('--data', default='tests/data/golden_set_500_zwy.json', help='Golden set 文件路径')
    
    args = parser.parse_args()
    
    run_e2e_tests(
        golden_set_path=args.data,
        user_name=args.user,
        max_tests=args.max_tests
    )
