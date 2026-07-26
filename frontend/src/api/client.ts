/**
 * Typed API client for the FastAPI backend (app_fastapi.py).
 *
 * All endpoints go through a single fetch wrapper with timeout and error
 * normalization. The SSE chat stream is parsed manually from the response
 * body (POST-SSE wire protocol: frames are `data: {json}\n\n`).
 */

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export type Role = 'user' | 'assistant'

export interface RagSource {
  title?: string
  source?: string
  url?: string
  score?: number
  snippet?: string
}

export interface ReplyItem {
  type: string
  content: string
}

/** Non-streaming POST /api/chat success payload. */
export interface ChatResult {
  replies: ReplyItem[]
  interrupted?: boolean
  intent?: string
  retry_count?: number
  emotion?: string
  emotion_intensity?: number
  next_action?: string
  session_id?: string
  cached?: boolean
  sources?: RagSource[]
  error?: string
}

/** Final SSE frame (`{"done": true, ...}`) metadata. */
export interface StreamMeta {
  done?: boolean
  reply_type?: string
  intent?: string
  emotion?: string
  emotion_intensity?: number
  session_id?: string
  sources?: RagSource[]
  [key: string]: unknown
}

export interface SessionSummary {
  session_id: string
  message_count: number
  last_activity?: string
  intents?: string[]
  preview?: string
}

export interface SessionDetail {
  messages: Array<{ role: Role; content: string }>
  intent?: string
  emotion?: string
  retry_count?: number
}

export interface AnalyticsData {
  total_conversations?: number
  avg_reply_length?: number
  ratings?: { total: number; average: number }
  tickets?: { total: number; by_priority: Record<string, number> }
  intents?: Record<string, number>
  emotions?: Record<string, number>
}

export interface HealthStatus {
  status: string
  version?: string
  uptime_seconds?: number
}

export interface OkResult {
  ok?: boolean
  error?: string
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number
  readonly kind: 'http' | 'timeout' | 'network' | 'abort'

  constructor(message: string, status = 0, kind: ApiError['kind'] = 'http') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.kind = kind
  }
}

const DEFAULT_TIMEOUT_MS = 15_000

interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  timeoutMs?: number
  signal?: AbortSignal
}

async function extractErrorMessage(res: Response): Promise<string> {
  try {
    const data: unknown = await res.clone().json()
    if (data && typeof data === 'object') {
      const record = data as Record<string, unknown>
      const msg = record.error ?? record.detail ?? record.message
      if (typeof msg === 'string' && msg) return msg
    }
  } catch {
    /* fall through to status text */
  }
  return `HTTP ${res.status} ${res.statusText}`.trim()
}

/** Perform a fetch with timeout; throws normalized ApiError on any failure. */
async function rawRequest(path: string, options: RequestOptions = {}): Promise<Response> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs)
  // Relay the caller's signal onto the fetch controller. Deliberately NOT
  // removed when headers arrive: for SSE the body outlives this function and
  // must still react to a user cancel.
  signal?.addEventListener('abort', () => controller.abort(signal.reason), { once: true })

  try {
    const res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
    if (!res.ok) {
      throw new ApiError(await extractErrorMessage(res), res.status, 'http')
    }
    return res
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && (err.name === 'AbortError' || err.name === 'TimeoutError')) {
      if (signal?.aborted) throw new ApiError('请求已取消', 0, 'abort')
      throw new ApiError('请求超时，请稍后重试', 0, 'timeout')
    }
    throw new ApiError(err instanceof Error ? err.message : '网络连接失败', 0, 'network')
  } finally {
    window.clearTimeout(timer)
  }
}

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const res = await rawRequest(path, options)
  return (await res.json()) as T
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export function sendChat(message: string, sessionId: string, timeoutMs = 120_000): Promise<ChatResult> {
  return requestJson<ChatResult>('/api/chat', {
    method: 'POST',
    body: { message, session_id: sessionId, stream: false },
    timeoutMs,
  })
}

export interface StreamCallbacks {
  /** Incremental token; append to the current assistant bubble. */
  onToken: (token: string) => void
  /** Backend progress hint, e.g. "analyzing". */
  onProgress?: (stage: string) => void
  /** Final metadata frame (`done: true`). */
  onDone?: (meta: StreamMeta) => void
  /** In-band error frame from the backend. */
  onError?: (message: string) => void
}

/**
 * POST /api/chat with stream=true and parse the SSE body.
 *
 * Frame format emitted by the backend (see app_fastapi._sse / legacy
 * run_agent_stream, mirrored by static/app.js):
 *   data: {"progress": "analyzing"}
 *   data: {"token": "..."}         (repeated)
 *   data: {"done": true, "reply_type": ..., "intent": ..., "emotion": ...}
 *   data: {"error": "..."}         (on failure)
 * A literal `data: [DONE]` terminator is also tolerated.
 *
 * Returns true if the stream produced a proper terminal frame. Throws
 * ApiError when the HTTP layer fails before/without any SSE content, so the
 * caller can fall back to non-streaming. An abort via `signal` returns
 * normally (partial text is kept by the caller).
 */
export async function streamChat(
  message: string,
  sessionId: string,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
): Promise<boolean> {
  const res = await rawRequest('/api/chat', {
    method: 'POST',
    body: { message, session_id: sessionId, stream: true },
    timeoutMs: 180_000,
    signal,
  })

  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream')) {
    // Backend answered with buffered JSON (e.g. guard rejection shape or
    // stream disabled); surface it through the non-stream path.
    const data = (await res.json()) as ChatResult
    if (data.error) {
      callbacks.onError?.(data.error)
      return true
    }
    for (const reply of data.replies ?? []) callbacks.onToken(reply.content)
    callbacks.onDone?.({ done: true, ...data })
    return true
  }

  if (!res.body) throw new ApiError('浏览器不支持流式响应', 0, 'network')

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finished = false

  const handleFrame = (frame: string): void => {
    for (const line of frame.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const payload = trimmed.slice(5).trim()
      if (!payload) continue
      if (payload === '[DONE]') {
        finished = true
        callbacks.onDone?.({ done: true })
        return
      }
      let parsed: unknown
      try {
        parsed = JSON.parse(payload)
      } catch {
        continue // tolerate malformed frames
      }
      if (!parsed || typeof parsed !== 'object') continue
      const frameData = parsed as Record<string, unknown>
      if (frameData.error !== undefined) {
        callbacks.onError?.(String(frameData.error))
        finished = true
        return
      }
      if (frameData.done) {
        finished = true
        callbacks.onDone?.(frameData as StreamMeta)
        return
      }
      if (typeof frameData.progress === 'string') {
        callbacks.onProgress?.(frameData.progress)
        continue
      }
      if (typeof frameData.token === 'string') {
        callbacks.onToken(frameData.token)
      } else if (typeof frameData.delta === 'string') {
        callbacks.onToken(frameData.delta)
      }
    }
  }

  try {
    while (!finished) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        handleFrame(frame)
        if (finished) break
      }
    }
    if (!finished && buffer.trim()) handleFrame(buffer)
  } catch (err) {
    if (signal?.aborted) return false // user cancel: keep partial text
    throw err instanceof ApiError ? err : new ApiError(err instanceof Error ? err.message : '流式连接中断', 0, 'network')
  } finally {
    reader.cancel().catch(() => undefined)
  }
  return finished
}

export function submitRating(sessionId: string, messageIndex: number, stars: number): Promise<OkResult> {
  return requestJson<OkResult>('/api/rating', {
    method: 'POST',
    body: { session_id: sessionId, message_index: messageIndex, stars },
  })
}

export function submitReaction(
  sessionId: string,
  messageId: string,
  emoji: string,
  active: boolean,
): Promise<OkResult> {
  return requestJson<OkResult>('/api/reaction', {
    method: 'POST',
    body: { session_id: sessionId, message_id: messageId, emoji, active },
  })
}

export function submitFeedback(
  sessionId: string,
  query: string,
  answer: string,
  rating: number,
  comment: string,
): Promise<OkResult> {
  return requestJson<OkResult>('/api/feedback', {
    method: 'POST',
    body: { session_id: sessionId, query, answer, rating, comment },
  })
}

export function fetchHealth(): Promise<HealthStatus> {
  return requestJson<HealthStatus>('/healthz', { timeoutMs: 8_000 })
}

export function fetchSession(sessionId: string): Promise<SessionDetail> {
  return requestJson<SessionDetail>(`/api/session/${encodeURIComponent(sessionId)}`)
}

/**
 * GET /api/sessions — only present in the legacy backend today; the FastAPI
 * port will add it later. Callers must treat ApiError(status 404) as "no
 * session directory available yet".
 */
export async function fetchSessions(): Promise<SessionSummary[]> {
  const data = await requestJson<{ sessions?: SessionSummary[] }>('/api/sessions')
  return data.sessions ?? []
}

/** GET /api/analytics — same availability caveat as fetchSessions. */
export function fetchAnalytics(): Promise<AnalyticsData> {
  return requestJson<AnalyticsData>('/api/analytics')
}
