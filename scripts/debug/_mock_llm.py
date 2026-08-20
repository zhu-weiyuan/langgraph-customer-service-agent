# -*- coding: utf-8 -*-
"""Mock LLM server (implements OpenAI-compatible API, returns instant canned replies)."""
import sys, json, asyncio, os
from aiohttp import web

async def models(request):
    return web.json_response({'data': [{'id': 'mock-model'}], 'object': 'list'})

async def chat_completions(request):
    body = await request.json()
    return web.json_response({
        'choices': [{'message': {'content': '您好，我是智能客服助手，请问有什么可以帮您的？', 'role': 'assistant'}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        'object': 'chat.completion',
    })

async def chat_stream(request):
    """SSE streaming response."""
    body = await request.json()
    response = web.StreamResponse(status=200, headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
    })
    await response.prepare(request)
    content = '您好，我是智能客服助手。'
    for chunk in content:
        data = json.dumps({
            'choices': [{'delta': {'content': chunk}, 'finish_reason': None}],
            'object': 'chat.completion.chunk',
        })
        await response.write(f'data: {data}\n\n'.encode())
        await asyncio.sleep(0.01)
    done = json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 15}})
    await response.write(f'data: {done}\n\n'.encode())
    await response.write(b'data: [DONE]\n\n')
    return response

app = web.Application()
app.router.add_get('/v1/models', models)
app.router.add_post('/v1/chat/completions', chat_completions)

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    web.run_app(app, port=port)
