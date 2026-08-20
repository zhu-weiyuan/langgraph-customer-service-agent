# -*- coding: utf-8 -*-
"""
灌数据脚本：用 golden set 的输入跑真实 LLM，把对话存进数据库。
用户名：zwy
"""
import sys, os, json, time, uuid
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import _build_core_graph
from langgraph.checkpoint.memory import MemorySaver
from agent.memory import save_conversation

USER_ID = "zwy"  # 固定用户名

def load_inputs(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def run_and_save(graph, test_cases, start_idx=0, batch_size=500):
    total = len(test_cases)
    done = 0
    errors = 0
    t0 = time.time()
    
    print(f"[START] 用户={USER_ID}, 总量={total}, 起始={start_idx}")
    print(f"[TIME]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 60)
    
    for i, case in enumerate(test_cases):
        if i < start_idx:
            continue
        if i >= start_idx + batch_size:
            break
        
        idx = i + 1
        input_text = case['input']
        case_id = case.get('id', f'case_{idx}')
        category = case.get('category', 'unknown')
        expected_intent = case.get('expected_intent', 'consult')
        
        session_id = f"zwy_e2e_{idx:04d}"
        
        try:
            # 跑一轮真实对话
            result = graph.invoke(
                {
                    "messages": [HumanMessage(content=input_text)],
                    "session_id": session_id,
                    "user_id": USER_ID,
                    "retry_count": 0,
                    "escalate": False,
                },
                config={"configurable": {"thread_id": session_id}}
            )
            
            # 取 bot 回复
            messages = result.get('messages', [])
            bot_reply = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    bot_reply = msg.content
                    break
            if not bot_reply:
                bot_reply = result.get('bot_reply', '')
            
            actual_intent = result.get('intent', expected_intent)
            emotion = result.get('emotion', 'neutral')
            intensity = result.get('emotion_intensity', 1)
            
            # 存到数据库（PostgreSQL）
            save_conversation(
                session_id=session_id,
                user_message=input_text,
                bot_reply=bot_reply,
                intent=actual_intent,
                emotion=emotion,
                emotion_intensity=intensity,
                resolved=True,
                user_id=USER_ID,
            )
            
            done += 1
            elapsed = time.time() - t0
            
            # 进度输出（每 50 条或每分钟）
            if idx % 50 == 0 or time.time() - t0 > (idx * 1.5):
                rate = done / (time.time() - t0 + 0.01)
                eta = (total - idx) / rate if rate > 0 else 0
                print(f"  [{idx}/{total}] {case_id:35s} | intent={actual_intent:10s} | {done} saved | {eta/60:.0f}min ETA")
            
        except Exception as e:
            errors += 1
            print(f"  [ERR {idx}/{total}] {case_id:35s} | {str(e)[:80]}")
            # 出错也存一条
            try:
                save_conversation(
                    session_id=session_id,
                    user_message=input_text,
                    bot_reply=f"[ERROR] {str(e)[:200]}",
                    intent=expected_intent,
                    user_id=USER_ID,
                )
            except Exception:
                pass
    
    elapsed_total = time.time() - t0
    print("-" * 60)
    print(f"[DONE] 用户={USER_ID}")
    print(f"  成功: {done}, 失败: {errors}")
    print(f"  耗时: {elapsed_total/60:.1f} 分钟")
    print(f"  速度: {done/elapsed_total:.1f} 条/秒" if elapsed_total > 0 else "")
    print(f"[TIME] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    # 载入 golden set
    golden_path = "tests/data/golden_set_500_zwy.json"
    if not os.path.exists(golden_path):
        print(f"[ERR] 找不到 {golden_path}")
        sys.exit(1)
    
    test_cases = load_inputs(golden_path)
    
    # 编译 graph（MemorySaver 用于执行）
    checkpointer = MemorySaver()
    graph = _build_core_graph()
    graph = graph.compile(checkpointer=checkpointer)
    
    # 参数解析支持 start_idx
    start = 0
    limit = len(test_cases)
    if len(sys.argv) > 1:
        start = int(sys.argv[1])
    if len(sys.argv) > 2:
        limit = int(sys.argv[2])
    
    run_and_save(graph, test_cases, start_idx=start, batch_size=limit)