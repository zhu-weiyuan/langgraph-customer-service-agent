# -*- coding: utf-8 -*-
"""
Web server for LangGraph Customer Service Agent (Real LLM version).

Flow: user asks -> bot replies -> ... -> user says bye -> satisfaction check -> resolve

Run: python app.py
Visit: http://localhost:7860
"""

import sys
import io
import json
from uuid import uuid4

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import build_graph
from http.server import HTTPServer, BaseHTTPRequestHandler
from langgraph.checkpoint.memory import MemorySaver

PORT = 7860

_shared_checkpointer = MemorySaver()
_graph = None


def init():
    global _graph
    _graph = build_graph()
    print(f"[Server] Agent initialized (Real LLM via llama.cpp)")


def run_agent(session_id, user_message):
    """Run the agent for a user message."""
    config = {"configurable": {"thread_id": session_id}}
    human_msg = HumanMessage(content=user_message)

    # Get current state to know how many messages already exist
    current_state = _graph.get_state(config)
    existing_count = 0
    if current_state and current_state.values:
        existing_count = len(current_state.values.get('messages', []))

    # Restore emotion state from previous turn if exists
    prev_emotion = 'neutral'
    prev_intensity = 1
    if existing_count > 0 and current_state and current_state.values:
        prev_emotion = current_state.values.get('emotion', 'neutral') or 'neutral'
        prev_intensity = current_state.values.get('emotion_intensity', 1) or 1

    input_data = {
        "messages": [human_msg],
        "session_id": session_id,
        "retry_count": 0,
        "emotion": prev_emotion,
        "emotion_intensity": prev_intensity,
    }

    all_new_messages = []
    interrupted = False

    try:
        for event in _graph.stream(input_data, config=config, stream_mode="values"):
            if event and event.get('messages'):
                new_msgs = event['messages'][existing_count:]
                all_new_messages.extend(new_msgs)
    except Exception as e:
        if "interrupt" in str(e).lower():
            interrupted = True

    # Get final state
    state = _graph.get_state(config)
    intent = 'unknown'
    retry_count = 0

    if state and state.values:
        intent = state.values.get('intent', 'unknown') or 'unknown'
        retry_count = state.values.get('retry_count', 0)
        emotion = state.values.get('emotion', 'neutral') or 'neutral'
        emotion_intensity = state.values.get('emotion_intensity', 1) or 1

    # Extract replies
    replies = []
    for msg in all_new_messages:
        if isinstance(msg, AIMessage):
            content = msg.content
            msg_type = _classify_message(content)
            replies.append({"type": msg_type, "content": content})

    next_action = "Active"
    if interrupted:
        next_action = "Escalated"

    return {
        "replies": replies,
        "interrupted": interrupted,
        "intent": intent,
        "retry_count": retry_count,
        "emotion": emotion,
        "emotion_intensity": emotion_intensity,
        "next_action": next_action,
        "session_id": session_id
    }


def _classify_message(content):
    """Classify bot message for UI styling."""
    if any(kw in content for kw in ["满意", "satisfied", "satisfy", "rate this"]):
        return "satisfaction"
    elif any(kw in content for kw in ["再见", "goodbye", "thank you for contacting", "祝您", "祝您生活愉快", "欢迎回来"]):
        return "closing"
    else:
        return "reply"


# ============================================================
# HTML UI (English)
# ============================================================

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LangGraph 智能客服 Agent</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f2f5; height: 100vh; display: flex; flex-direction: column; }
  .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
  .header h1 { font-size: 18px; font-weight: 600; }
  .status { font-size: 13px; opacity: 0.9; }
  .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4ade80; margin-right: 6px; }
  .toolbar { background: white; padding: 10px 24px; display: flex; gap: 10px; border-bottom: 1px solid #e5e7eb; }
  .toolbar button { padding: 8px 16px; border: 1px solid #d1d5db; background: white; border-radius: 6px; cursor: pointer; font-size: 13px; color: #374151; transition: all 0.15s; }
  .toolbar button:hover { background: #f3f4f6; }
  .toolbar button.primary { background: #667eea; color: white; border-color: #667eea; }
  .toolbar button.danger { background: #ef4444; color: white; border-color: #ef4444; }
  .test-cases { background: #f9fafb; padding: 10px 24px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; border-bottom: 1px solid #e5e7eb; }
  .test-cases .label { font-size: 12px; color: #6b7280; margin-right: 4px; font-weight: 600; }
  .tc-btn { padding: 5px 12px; background: white; border: 1px solid #d1d5db; border-radius: 16px; font-size: 12px; cursor: pointer; transition: all 0.15s; }
  .tc-btn:hover { background: #f3f4f6; }
  .tc-btn.green { border-color: #86efac; color: #166534; background: #f0fdf4; }
  .tc-btn.red { border-color: #fca5a5; color: #991b1b; background: #fef2f2; }
  .tc-btn.blue { border-color: #93c5fd; color: #1e40af; background: #eff6ff; }
  .chat-container { flex: 1; overflow-y: auto; padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }
  .message { display: flex; gap: 12px; max-width: 75%; animation: fadeIn 0.3s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .message.user { align-self: flex-end; flex-direction: row-reverse; }
  .message.bot { align-self: flex-start; }
  .avatar { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
  .message.user .avatar { background: #667eea; }
  .message.bot .avatar { background: linear-gradient(135deg, #f093fb, #f5576c); }
  .bubble { padding: 12px 16px; border-radius: 16px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  .message.user .bubble { background: #667eea; color: white; border-bottom-right-radius: 4px; }
  .message.bot .bubble { background: white; color: #1f2937; border-bottom-left-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .message.bot .bubble.satisfaction { background: #fef3c7; border: 1px solid #f59e0b; }
  .message.bot .bubble.closing { background: #dcfce7; border: 1px solid #22c55e; }
  .system-msg { align-self: center; padding: 6px 16px; background: #f3f4f6; border-radius: 12px; font-size: 12px; color: #6b7280; }
  .input-area { background: white; padding: 16px 24px; border-top: 1px solid #e5e7eb; display: flex; gap: 12px; }
  .input-area input { flex: 1; padding: 12px 16px; border: 1px solid #d1d5db; border-radius: 24px; font-size: 14px; outline: none; transition: border-color 0.15s; }
  .input-area input:focus { border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
  .input-area button { padding: 12px 24px; background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; border-radius: 24px; font-size: 14px; cursor: pointer; }
  .input-area button:hover { opacity: 0.9; }
  .input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
  .info-bar { background: #f8fafc; padding: 8px 24px; font-size: 12px; color: #6b7280; display: flex; gap: 20px; border-top: 1px solid #e5e7eb; overflow-x: auto; }
  .info-bar span { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .info-bar .label { color: #9ca3af; }
  .typing-indicator { display: flex; gap: 4px; padding: 12px 16px; align-items: center; }
  .typing-indicator .dot { width: 8px; height: 8px; background: #9ca3af; border-radius: 50%; animation: bounce 1.4s infinite; }
  .typing-indicator .dot:nth-child(2) { animation-delay: 0.2s; }
  .typing-indicator .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-8px); } }

  /* Typing cursor for character-by-character animation */
  .typing-cursor::after {
    content: '▊';
    animation: blink 0.8s infinite;
    color: #667eea;
    font-weight: 300;
  }
  @keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }

  /* Memory indicator */
  .memory-badge { display: inline-block; font-size: 10px; padding: 2px 6px; background: #e0e7ff; color: #4338ca; border-radius: 8px; margin-left: 8px; }

  /* Mobile responsive */
  @media (max-width: 640px) {
    .header { padding: 12px 16px; }
    .header h1 { font-size: 15px; }
    .toolbar { padding: 8px 16px; gap: 6px; flex-wrap: wrap; }
    .test-cases { padding: 8px 16px; }
    .chat-container { padding: 16px;
      gap: 12px; }
    .message { max-width: 88%; }
    .input-area { padding: 12px 16px; }
    .info-bar { padding: 6px 16px; font-size: 11px; gap: 12px; }
    .avatar { width: 30px; height: 30px; font-size: 15px; }
    .bubble { padding: 10px 14px; font-size: 13px; }
  }

  /* Scrollbar styling */
  .chat-container::-webkit-scrollbar { width: 6px; }
  .chat-container::-webkit-scrollbar-track { background: transparent; }
  .chat-container::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }
  .chat-container::-webkit-scrollbar-thumb:hover { background: #9ca3af; }
</style>
</head>
<body>

<div class="header">
  <h1>LangGraph 智能客服 Agent</h1>
  <div class="status"><span class="dot"></span>在线 (本地 LLM)</div>
</div>

<div class="toolbar">
  <button class="primary" onclick="newSession()">新会话</button>
  <button onclick="clearChat()">清空聊天</button>
  <button class="danger" onclick="resetAll()">重置全部</button>
</div>

<div class="test-cases">
  <span class="label">测试：</span>
  <button class="tc-btn blue" onclick="quickTest('产品怎么用？')">产品咨询</button>
  <button class="tc-btn red" onclick="quickTest('我要投诉，产品质量太差了')">投诉</button>
  <button class="tc-btn" onclick="quickTest('你好')">打招呼</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <span class="label">结束：</span>
  <button class="tc-btn green" onclick="quickTest('谢谢，没问题了')">感谢并结束</button>
  <button class="tc-btn red" onclick="quickTest('再见')">说再见</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <span class="label">反馈：</span>
  <button class="tc-btn green" onclick="quickTest('满意')">满意</button>
  <button class="tc-btn red" onclick="quickTest('不满意')">不满意</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <button class="tc-btn blue" onclick="runFullFlow()">自动完整流程</button>
</div>

<div class="chat-container" id="chatContainer"></div>

<div class="info-bar">
  <span><span class="label">Session:</span> <span id="infoSession">-</span></span>
  <span><span class="label">Intent:</span> <span id="infoIntent">-</span></span>
  <span><span class="label">Retries:</span> <span id="infoRetries">0</span></span>
  <span><span class="label">Emotion:</span> <span id="infoEmotion">-</span></span>
  <span><span class="label">Status:</span> <span id="infoStatus">Active</span></span>
</div>

<div class="input-area">
  <input type="text" id="messageInput" placeholder="输入消息..." autocomplete="off" />
  <button id="sendBtn" onclick="sendMessage()">发送</button>
</div>

<script>
let currentSession = null;
let isProcessing = false;

const chatContainer = document.getElementById('chatContainer');
const messageInput = document.getElementById('messageInput');
const sendBtn = document.getElementById('sendBtn');

messageInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !isProcessing) sendMessage();
});

function addMessage(role, content, type, animate) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '\u{1F464}' : '\u{1F916}';
  const bubble = document.createElement('div');
  bubble.className = `bubble ${type || ''}`;
  div.appendChild(avatar);
  div.appendChild(bubble);
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;

  if (animate && role === 'bot') {
    typeWriter(bubble, content, 20);
  } else {
    bubble.textContent = content;
  }
}

function typeWriter(element, text, speed) {
  element.classList.add('typing-cursor');
  let i = 0;
  const len = text.length;
  function type() {
    if (i < len) {
      element.textContent = text.substring(0, i + 1);
      i++;
      chatContainer.scrollTop = chatContainer.scrollHeight;
      setTimeout(type, speed);
    } else {
      element.classList.remove('typing-cursor');
    }
  }
  type();
}

function addSystemMsg(text) {
  const div = document.createElement('div');
  div.className = 'system-msg';
  div.textContent = text;
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'message bot';
  div.id = 'typingIndicator';
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = '\u{1F916}';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = '<div class="typing-indicator"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>';
  div.appendChild(avatar);
  div.appendChild(bubble);
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

async function sendMessage(text) {
  if (isProcessing) return;
  const message = text || messageInput.value.trim();
  if (!message) return;
  messageInput.value = '';
  addMessage('user', message);

  isProcessing = true;
  sendBtn.disabled = true;
  addTyping();

  try {
    const session = currentSession || crypto.randomUUID();
    if (!currentSession) {
      currentSession = session;
      document.getElementById('infoSession').textContent = session.slice(0, 8) + '...';
    }

    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: session })
    });

    const data = await response.json();
    removeTyping();

    if (data.error) {
      addMessage('bot', 'Error: ' + data.error, '', false);
    } else {
      for (const reply of data.replies) {
        const typeMap = { satisfaction: 'satisfaction', closing: 'closing' };
        addMessage('bot', reply.content, typeMap[reply.type] || '', true);
      }

      if (data.intent) document.getElementById('infoIntent').textContent = data.intent;
      if (data.retry_count !== undefined) document.getElementById('infoRetries').textContent = data.retry_count;
      if (data.emotion) {
        const emojiMap = { neutral: '😐', angry: '😠', sad: '😢', anxious: '😰', happy: '😊' };
        const emoji = emojiMap[data.emotion] || '😐';
        document.getElementById('infoEmotion').textContent = emoji + ' ' + data.emotion + (data.emotion_intensity ? '(' + data.emotion_intensity + ')' : '');
      }
      document.getElementById('infoStatus').textContent = data.interrupted ? 'Escalated' : 'Active';
    }
  } catch (err) {
    removeTyping();
    addMessage('bot', 'Connection error: ' + err.message, '');
  }

  isProcessing = false;
  sendBtn.disabled = false;
  messageInput.focus();
}

function newSession() {
  currentSession = crypto.randomUUID();
  document.getElementById('infoSession').textContent = currentSession.slice(0, 8) + '...';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoStatus').textContent = 'Active';
  addSystemMsg('新会话已启动');
}

function clearChat() { chatContainer.innerHTML = ''; }

function resetAll() {
  currentSession = null;
  chatContainer.innerHTML = '';
  document.getElementById('infoSession').textContent = '-';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoStatus').textContent = 'Active';
}

function quickTest(text) { messageInput.value = text; sendMessage(text); }

async function runFullFlow() {
  clearChat();
  newSession();
  await new Promise(r => setTimeout(r, 500));

  const steps = [
    { msg: '产品怎么用？', label: '步骤1：咨询产品用法', delay: 12000 },
    { msg: '谢谢，没问题了', label: '步骤2：结束对话 → 满意度检查', delay: 12000 },
    { msg: '满意', label: '步骤3：满意 → 结束语', delay: 12000 },
  ];

  for (const step of steps) {
    addSystemMsg(step.label);
    await new Promise(r => setTimeout(r, 500));
    messageInput.value = step.msg;
    await sendMessage(step.msg);
    await new Promise(r => setTimeout(r, step.delay));
  }

  addSystemMsg('完整流程演示完成！');
}
</script>

</body>
</html>"""


class ChatHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(CHAT_HTML.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/api/chat':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            user_message = data.get('message', '')
            session_id = data.get('session_id', str(uuid4()))

            try:
                result = run_agent(session_id, user_message)
                response = json.dumps(result, ensure_ascii=False)
            except Exception as e:
                import traceback
                traceback.print_exc()
                response = json.dumps({'error': str(e)}, ensure_ascii=False)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(response.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def main():
    init()
    server = HTTPServer(('0.0.0.0', PORT), ChatHandler)
    print(f"[Server] Running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
