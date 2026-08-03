#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal FastAPI server for customer service agent."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Customer Service Agent")


class ChatRequest(BaseModel):
    message: str
    history: list = []


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>智能客服系统</title>
        <meta charset="utf-8">
        <style>
            body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            #chat { border: 1px solid #ddd; border-radius: 8px; padding: 20px; min-height: 400px; margin-bottom: 20px; }
            .user { color: #1a73e8; margin: 10px 0; }
            .assistant { color: #333; margin: 10px 0; padding: 10px; background: #f5f5f5; border-radius: 4px; }
            input { width: 70%; padding: 10px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; }
            button { padding: 10px 20px; font-size: 16px; background: #1a73e8; color: white; border: none; border-radius: 4px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h1>智能客服系统</h1>
        <div id="chat"></div>
        <input type="text" id="input" placeholder="请输入您的问题..." onkeypress="if(event.key==='Enter')send()">
        <button onclick="send()">发送</button>
        <script>
            const chat = document.getElementById('chat');
            const input = document.getElementById('input');
            async function send() {
                const msg = input.value.trim();
                if (!msg) return;
                input.value = '';
                chat.innerHTML += `<div class="user">你：${msg}</div>`;
                try {
                    const res = await fetch('/chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({message: msg})
                    });
                    const data = await res.json();
                    chat.innerHTML += `<div class="assistant">客服：${data.response}</div>`;
                } catch (e) {
                    chat.innerHTML += `<div class="assistant">客服：[错误] ${e.message}</div>`;
                }
                chat.scrollTop = chat.scrollHeight;
            }
        </script>
    </body>
    </html>
    """


@app.post("/chat")
async def chat(request: ChatRequest):
    from agent.rag import build_context
    from agent.llm_client import LLMClient

    llm = LLMClient()

    # Get RAG context
    context = build_context(request.message)

    # Generate reply
    response = llm.generate_reply(
        user_message=request.message,
        context=context,
    )

    return {"response": response}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
