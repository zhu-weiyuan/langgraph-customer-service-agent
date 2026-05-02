# -*- coding: utf-8 -*-
"""
Simple web server for the LangGraph Customer Service Agent.

Provides a chat interface to test the agent in a real browser,
simulating actual user experience.

Run: python app.py
Visit: http://localhost:7860
"""

import sys
import io
import json
from uuid import uuid4

# Windows UTF-8 compatibility
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from langchain_core.messages import HumanMessage, AIMessage
from agent.graph import _build_core_graph
from langgraph.checkpoint.memory import MemorySaver

# Global graph instance
PORT = 7860
checkpointer = MemorySaver()
graph = None


def init_graph():
    """Initialize the LangGraph agent with in-memory checkpointer."""
    global graph
    g = _build_core_graph()
    graph = g.compile(checkpointer=checkpointer)
    print(f"[Server] Agent initialized")


def run_agent(session_id, user_message):
    """Run the agent with a user message and return a clean response.

    Key design: The graph runs to completion in one stream call.
    We extract only the meaningful messages for the UI:
    - First bot reply (or last retry reply)
    - Satisfaction check prompt
    - Escalation message if triggered
    """
    config = {"configurable": {"thread_id": session_id}}

    human_msg = HumanMessage(content=user_message)

    # Check if session is interrupted (needs resume)
    current_state = graph.get_state(config)

    if current_state and current_state.next:
        input_data = {"messages": [human_msg]}
    else:
        input_data = {
            "messages": [human_msg],
            "session_id": session_id,
            "retry_count": 0,
            "escalate": False
        }

    all_messages = []

    try:
        for event in graph.stream(input_data, config=config, stream_mode="values"):
            if event and event.get('messages'):
                all_messages.extend(event['messages'])

        final_state = graph.get_state(config)

    except Exception as e:
        if "interrupt" in str(e).lower() or "Interrupt" in str(e):
            return {
                "replies": [{"type": "escalation", "content": "Session suspended for human intervention"}],
                "interrupted": True,
                "session_id": session_id
            }
        raise

    # --- Post-process: extract meaningful messages for UI ---
    # The graph runs all nodes in one stream call. We get ALL messages.
    # Filter to show only what the user should see.

    bot_replies = []
    satisfaction_shown = False
    escalation_shown = False

    for msg in all_messages:
        if isinstance(msg, AIMessage):
            content = msg.content

            if "转接人工" in content or "人工客服" in content:
                if not escalation_shown:
                    bot_replies.append({"type": "escalation", "content": content})
                    escalation_shown = True
            elif "满意" in content and "请问" in content:
                if not satisfaction_shown:
                    bot_replies.append({"type": "satisfaction_check", "content": content})
                    satisfaction_shown = True
            else:
                # Regular reply — only keep the LAST one (after retries)
                # Remove previous regular replies
                bot_replies = [r for r in bot_replies if r["type"] != "reply"]
                bot_replies.append({"type": "reply", "content": content})

    # Build final state info
    intent = 'unknown'
    retry_count = 0
    satisfaction = None
    is_escalated = False

    if final_state and final_state.values:
        intent = final_state.values.get('intent', 'unknown') or 'unknown'
        retry_count = final_state.values.get('retry_count', 0)
        satisfaction = final_state.values.get('satisfaction')
        is_escalated = final_state.values.get('escalate', False)

    # Determine next action hint
    if is_escalated:
        next_action = "Escalated to human"
    elif satisfaction is True:
        next_action = "Resolved"
    elif retry_count >= 3:
        next_action = "Max retries reached"
    else:
        next_action = "Waiting for feedback"

    return {
        "replies": bot_replies,
        "interrupted": False,
        "intent": intent,
        "retry_count": retry_count,
        "satisfaction": satisfaction,
        "next_action": next_action,
        "session_id": session_id
    }


# ============================================================
# HTML UI
# ============================================================

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LangGraph Customer Service Agent - Test</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }

  .header {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }

  .header h1 { font-size: 18px; font-weight: 600; }
  .header .status { font-size: 13px; opacity: 0.9; }
  .header .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4ade80; margin-right: 6px; }

  .toolbar {
    background: white;
    padding: 10px 24px;
    display: flex;
    gap: 10px;
    border-bottom: 1px solid #e5e7eb;
  }

  .toolbar button {
    padding: 8px 16px;
    border: 1px solid #d1d5db;
    background: white;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    color: #374151;
    transition: all 0.15s;
  }

  .toolbar button:hover { background: #f3f4f6; border-color: #9ca3af; }
  .toolbar button.primary { background: #667eea; color: white; border-color: #667eea; }
  .toolbar button.primary:hover { background: #5b6fe0; }
  .toolbar button.danger { background: #ef4444; color: white; border-color: #ef4444; }
  .toolbar button.danger:hover { background: #dc2626; }

  .test-cases {
    background: #f9fafb;
    padding: 10px 24px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
    border-bottom: 1px solid #e5e7eb;
  }

  .test-cases .label { font-size: 12px; color: #6b7280; margin-right: 4px; font-weight: 600; }

  .test-case-btn {
    padding: 5px 12px;
    background: white;
    border: 1px solid #d1d5db;
    border-radius: 16px;
    font-size: 12px;
    color: #374151;
    cursor: pointer;
    transition: all 0.15s;
  }

  .test-case-btn:hover { background: #f3f4f6; border-color: #9ca3af; }
  .test-case-btn.green { border-color: #86efac; color: #166534; background: #f0fdf4; }
  .test-case-btn.green:hover { background: #dcfce7; }
  .test-case-btn.red { border-color: #fca5a5; color: #991b1b; background: #fef2f2; }
  .test-case-btn.red:hover { background: #fee2e2; }
  .test-case-btn.blue { border-color: #93c5fd; color: #1e40af; background: #eff6ff; }
  .test-case-btn.blue:hover { background: #dbeafe; }

  .chat-container {
    flex: 1;
    overflow-y: auto;
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .message {
    display: flex;
    gap: 12px;
    max-width: 75%;
    animation: fadeIn 0.3s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .message.user { align-self: flex-end; flex-direction: row-reverse; }
  .message.bot { align-self: flex-start; }

  .avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    flex-shrink: 0;
  }

  .message.user .avatar { background: #667eea; }
  .message.bot .avatar { background: linear-gradient(135deg, #f093fb, #f5576c); }

  .bubble {
    padding: 12px 16px;
    border-radius: 16px;
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .message.user .bubble {
    background: #667eea;
    color: white;
    border-bottom-right-radius: 4px;
  }

  .message.bot .bubble {
    background: white;
    color: #1f2937;
    border-bottom-left-radius: 4px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
  }

  .message.bot .bubble.satisfaction {
    background: #fef3c7;
    border: 1px solid #f59e0b;
  }

  .message.bot .bubble.escalation {
    background: #fee2e2;
    border: 1px solid #ef4444;
  }

  .system-msg {
    align-self: center;
    padding: 6px 16px;
    background: #f3f4f6;
    border-radius: 12px;
    font-size: 12px;
    color: #6b7280;
  }

  .input-area {
    background: white;
    padding: 16px 24px;
    border-top: 1px solid #e5e7eb;
    display: flex;
    gap: 12px;
  }

  .input-area input {
    flex: 1;
    padding: 12px 16px;
    border: 1px solid #d1d5db;
    border-radius: 24px;
    font-size: 14px;
    outline: none;
    transition: border-color 0.15s;
  }

  .input-area input:focus { border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }

  .input-area button {
    padding: 12px 24px;
    background: linear-gradient(135deg, #667eea, #764ba2);
    color: white;
    border: none;
    border-radius: 24px;
    font-size: 14px;
    cursor: pointer;
    transition: opacity 0.15s;
  }

  .input-area button:hover { opacity: 0.9; }
  .input-area button:disabled { opacity: 0.5; cursor: not-allowed; }

  .info-bar {
    background: #f8fafc;
    padding: 8px 24px;
    font-size: 12px;
    color: #6b7280;
    display: flex;
    gap: 20px;
    border-top: 1px solid #e5e7eb;
  }

  .info-bar span { display: flex; align-items: center; gap: 4px; }
  .info-bar .label { color: #9ca3af; }

  .typing-indicator {
    display: flex;
    gap: 4px;
    padding: 12px 16px;
    align-items: center;
  }

  .typing-indicator .dot {
    width: 8px;
    height: 8px;
    background: #9ca3af;
    border-radius: 50%;
    animation: bounce 1.4s infinite;
  }

  .typing-indicator .dot:nth-child(2) { animation-delay: 0.2s; }
  .typing-indicator .dot:nth-child(3) { animation-delay: 0.4s; }

  @keyframes bounce {
    0%, 60%, 100% { transform: translateY(0); }
    30% { transform: translateY(-8px); }
  }

  .interrupted-banner {
    background: #fef3c7;
    border: 1px solid #f59e0b;
    color: #92400e;
    padding: 12px 24px;
    font-size: 13px;
    text-align: center;
    display: none;
  }

  .interrupted-banner.show { display: block; }

  .retry-info {
    background: #eff6ff;
    border: 1px solid #93c5fd;
    color: #1e40af;
    padding: 8px 16px;
    font-size: 12px;
    text-align: center;
    border-radius: 8px;
    max-width: 400px;
    align-self: center;
  }
</style>
</head>
<body>

<div class="header">
  <h1>LangGraph Customer Service Agent</h1>
  <div class="status"><span class="dot"></span>Online</div>
</div>

<div class="toolbar">
  <button class="primary" onclick="newSession()">New Session</button>
  <button onclick="clearChat()">Clear Chat</button>
  <button class="danger" onclick="resetAll()">Reset All</button>
</div>

<div class="test-cases">
  <span class="label">Quick Test:</span>
  <button class="test-case-btn blue" onclick="quickTest('产品怎么用？')">Product Usage</button>
  <button class="test-case-btn red" onclick="quickTest('我要投诉，产品质量太差了')">Complaint</button>
  <button class="test-case-btn" onclick="quickTest('你好')">Greeting</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <span class="label">Feedback:</span>
  <button class="test-case-btn green" onclick="quickTest('满意')">Satisfied</button>
  <button class="test-case-btn red" onclick="quickTest('不满意')">Not Satisfied</button>
  <button class="test-case-btn red" onclick="quickTest('非常不满意，要投诉')">Force Escalation</button>
  <span style="width:1px;height:20px;background:#d1d5db;margin:0 4px"></span>
  <button class="test-case-btn blue" onclick="runFullFlow()">Auto Full Flow</button>
</div>

<div class="interrupted-banner" id="interruptBanner">
  Session suspended for human intervention. Send a message to resume.
</div>

<div class="chat-container" id="chatContainer"></div>

<div class="info-bar">
  <span><span class="label">Session:</span> <span id="infoSession">-</span></span>
  <span><span class="label">Intent:</span> <span id="infoIntent">-</span></span>
  <span><span class="label">Retries:</span> <span id="infoRetries">0</span></span>
  <span><span class="label">Status:</span> <span id="infoStatus">Active</span></span>
  <span><span class="label">Next:</span> <span id="infoNext">-</span></span>
</div>

<div class="input-area">
  <input type="text" id="messageInput" placeholder="Type your message..." autocomplete="off" />
  <button id="sendBtn" onclick="sendMessage()">Send</button>
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

function addMessage(role, content, type = 'normal') {
  const div = document.createElement('div');
  div.className = `message ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '👤' : '🤖';

  const bubble = document.createElement('div');
  bubble.className = `bubble ${type}`;
  bubble.textContent = content;

  div.appendChild(avatar);
  div.appendChild(bubble);
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addSystemMsg(text) {
  const div = document.createElement('div');
  div.className = 'system-msg';
  div.textContent = text;
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addRetryInfo(count, max) {
  const div = document.createElement('div');
  div.className = 'retry-info';
  div.textContent = `Retry ${count}/${max} — Bot regenerated response`;
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'message bot';
  div.id = 'typingIndicator';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = '🤖';

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
      addMessage('bot', 'Error: ' + data.error, 'escalation');
    } else {
      // Show retry info if retries happened
      if (data.retry_count > 0 && data.replies.length > 1) {
        addRetryInfo(data.retry_count, 3);
      }

      // Display bot replies in order
      for (const reply of data.replies) {
        if (reply.type === 'satisfaction_check') {
          addMessage('bot', reply.content, 'satisfaction');
        } else if (reply.type === 'escalation') {
          addMessage('bot', reply.content, 'escalation');
        } else {
          addMessage('bot', reply.content);
        }
      }

      // Update info bar
      if (data.intent) document.getElementById('infoIntent').textContent = data.intent;
      if (data.retry_count !== undefined) document.getElementById('infoRetries').textContent = data.retry_count;
      if (data.next_action) document.getElementById('infoNext').textContent = data.next_action;

      if (data.interrupted) {
        document.getElementById('infoStatus').textContent = 'Suspended';
        document.getElementById('interruptBanner').classList.add('show');
      } else {
        document.getElementById('infoStatus').textContent = 'Active';
        document.getElementById('interruptBanner').classList.remove('show');
      }
    }
  } catch (err) {
    removeTyping();
    addMessage('bot', 'Connection error: ' + err.message, 'escalation');
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
  document.getElementById('infoNext').textContent = '-';
  document.getElementById('interruptBanner').classList.remove('show');
  addSystemMsg('New session started');
}

function clearChat() {
  chatContainer.innerHTML = '';
}

function resetAll() {
  currentSession = null;
  chatContainer.innerHTML = '';
  document.getElementById('infoSession').textContent = '-';
  document.getElementById('infoIntent').textContent = '-';
  document.getElementById('infoRetries').textContent = '0';
  document.getElementById('infoStatus').textContent = 'Active';
  document.getElementById('infoNext').textContent = '-';
  document.getElementById('interruptBanner').classList.remove('show');
}

function quickTest(text) {
  messageInput.value = text;
  sendMessage(text);
}

async function runFullFlow() {
  clearChat();
  newSession();
  await new Promise(r => setTimeout(r, 500));

  const steps = [
    { msg: '产品怎么用？', label: 'Step 1: User asks about product', delay: 2500 },
    { msg: '不满意', label: 'Step 2: Not satisfied → retry', delay: 2500 },
    { msg: '满意', label: 'Step 3: Satisfied → resolved', delay: 2500 },
  ];

  for (const step of steps) {
    addSystemMsg(step.label);
    await new Promise(r => setTimeout(r, 300));
    messageInput.value = step.msg;
    await sendMessage(step.msg);
    await new Promise(r => setTimeout(r, step.delay));
  }

  addSystemMsg('Full flow demo complete!');
}
</script>

</body>
</html>"""


from http.server import HTTPServer, BaseHTTPRequestHandler

class ChatHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the chat UI and API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

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
                print(f"[Error] {e}")
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
    init_graph()
    from http.server import HTTPServer
    server = HTTPServer(('0.0.0.0', PORT), ChatHandler)
    print(f"[Server] Chat UI running at http://localhost:{PORT}")
    print(f"[Server] Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
