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

/**
 * Session row as returned by the multi-user backend
 * (GET /api/sessions → sessions[]): history for the *current* logged-in user.
 */
export interface SessionSummary {
  session_id: string
  title?: string
  created_at?: string
  last_active?: string
  message_count: number
}

// ---------------------------------------------------------------------------
// Auth types
// ---------------------------------------------------------------------------

export interface LoginResult {
  ok: boolean
  user_id: string
  registered?: boolean
  access_token: string | null
  token_type?: string
  session_id?: string
  error?: string
}

export interface RegisterResult {
  ok: boolean
  user_id: string
  created?: boolean
  access_token: string | null
  token_type?: string
  error?: string
}

export interface MeResult {
  user_id: string
  tenant_id: string
  auth_scheme: string
  authenticated: boolean
}

export interface RefreshResult {
  ok: boolean
  user_id: string
  tenant_id: string
  access_token: string
  token_type?: string
}

/** One long-term memory row (GET /api/memory → memories[]). */
export interface MemoryItem {
  id: string
  content: string
  kind: string
  importance: number
  created_at?: string
}

export interface DeleteMemoryResult {
  ok?: boolean
  deleted?: string
  error?: string
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

/** Detailed service health returned by GET /api/health. */
export interface ServiceHealth {
  ok: boolean
  service?: string
  version?: string
  port?: number
  uptime_seconds?: number
  llm?: { reachable?: boolean }
  redis?: { available?: boolean }
  rate_limiter?: {
    degraded?: boolean
    degraded_count?: number
    active_concurrency?: number
    max_concurrency?: number
  }
  database?: {
    conversations?: number
    total_ratings?: number
    avg_rating?: number
  }
}

export interface ReadinessCheck {
  ok: boolean
  pgvector?: boolean
  degraded_mode?: boolean
  error?: string
  [key: string]: unknown
}

/** Dependency readiness returned by GET /api/ready. */
export interface ReadinessStatus {
  ready: boolean
  checks: Record<string, ReadinessCheck>
}

export interface PrometheusSample {
  name: string
  labels: Record<string, string>
  value: number
}

export interface ObservabilitySnapshot {
  health: ServiceHealth
  readiness: ReadinessStatus
  metrics: {
    samples: PrometheusSample[]
    requests_total: number
    errors_total: number
    avg_latency_ms: number
    rate_limit_total: number
    llm_requests_total: number
    llm_errors_total: number
    llm_input_tokens: number
    llm_output_tokens: number
    llm_cost_yuan: number
    rag_hit_ratio: number | null
    rag_queries_total: number
    rag_hits_total: number
    feedback_total: number
    endpoint_requests: Record<string, number>
    endpoint_errors: Record<string, number>
  }
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
  method?: 'GET' | 'POST' | 'DELETE'
  body?: unknown
  timeoutMs?: number
  signal?: AbortSignal
  headers?: Record<string, string>
  retryAfterRefresh?: boolean
  skipAuthRefresh?: boolean
}

// ---------------------------------------------------------------------------
// Auth identity injection
//
// A single module-level identity, set by the ui store after login/restore.
// Every request through rawRequest() carries it automatically:
//   - JWT available  → `Authorization: Bearer <token>`
//   - JWT_SECRET off → `X-User-Id: <user_id>` (backend derives identity `u:<id>`)
// Kept out of the Pinia store so client.ts stays store-agnostic (no cycle).
// ---------------------------------------------------------------------------

interface AuthIdentity {
  userId: string
  token: string | null
}

let authIdentity: AuthIdentity | null = null

/** Set (or clear, with null) the identity attached to every subsequent request. */
export function setAuthIdentity(identity: AuthIdentity | null): void {
  authIdentity = identity
}

/** Build the auth headers for the current identity (empty when anonymous). */
function authHeaders(): Record<string, string> {
  if (!authIdentity) return {}

  // A browser-controlled user id is not an authentication credential. The
  // backend only accepts it when its explicit local-development compatibility
  // switch is enabled; production identity always comes from the JWT.
  const headers: Record<string, string> = {}
  if (authIdentity.token) headers.Authorization = `Bearer ${authIdentity.token}`
  return headers
}

let refreshInFlight: Promise<RefreshResult | null> | null = null

/** Replace the in-memory access JWT after a successful cookie-backed refresh. */
function applyRefreshedAccessToken(result: RefreshResult): void {
  if (authIdentity) {
    authIdentity = { userId: result.user_id, token: result.access_token }
  }
}

async function refreshAccessToken(): Promise<RefreshResult | null> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await fetch('/api/auth/refresh', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
        })
        if (!res.ok) return null
        const result = (await res.json()) as RefreshResult
        if (!result.access_token || !result.user_id) return null
        applyRefreshedAccessToken(result)
        return result
      } catch {
        return null
      } finally {
        refreshInFlight = null
      }
    })()
  }
  return refreshInFlight
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
  const {
    method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal, headers: extraHeaders,
    retryAfterRefresh = true, skipAuthRefresh = false,
  } = options
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs)
  // Relay the caller's signal onto the fetch controller. Deliberately NOT
  // removed when headers arrive: for SSE the body outlives this function and
  // must still react to a user cancel.
  signal?.addEventListener('abort', () => controller.abort(signal.reason), { once: true })

  try {
    const headers: Record<string, string> = { ...authHeaders(), ...(extraHeaders ?? {}) }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    let res = await fetch(path, {
      method,
      headers: Object.keys(headers).length ? headers : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
      signal: controller.signal,
    })
    // One shared refresh prevents a burst of expired requests from rotating the
    // session repeatedly. The original request is retried exactly once.
    if (res.status === 401 && retryAfterRefresh && !skipAuthRefresh && path !== '/api/auth/refresh') {
      const refreshed = await refreshAccessToken()
      if (refreshed) {
        const retryHeaders: Record<string, string> = { ...authHeaders(), ...(extraHeaders ?? {}) }
        if (body !== undefined) retryHeaders['Content-Type'] = 'application/json'
        res = await fetch(path, {
          method,
          headers: Object.keys(retryHeaders).length ? retryHeaders : undefined,
          body: body !== undefined ? JSON.stringify(body) : undefined,
          credentials: 'same-origin',
          signal: controller.signal,
        })
      }
    }
    if (!res.ok) {
      throw new ApiError(await extractErrorMessage(res), res.status, 'http')
    }
    return res
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && (err.name === 'AbortError' || err.name === 'TimeoutError')) {
      if (signal?.aborted) throw new ApiError('?????', 0, 'abort')
      throw new ApiError('??????????', 0, 'timeout')
    }
    throw new ApiError(err instanceof Error ? err.message : '??????', 0, 'network')
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

export function fetchServiceHealth(): Promise<ServiceHealth> {
  return requestJson<ServiceHealth>('/api/health', { timeoutMs: 8_000 })
}

export function fetchReadiness(): Promise<ReadinessStatus> {
  return requestJson<ReadinessStatus>('/api/ready', { timeoutMs: 8_000 })
}

function parsePrometheusLabels(raw: string): Record<string, string> {
  const labels: Record<string, string> = {}
  const pattern = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"/g
  for (const match of raw.matchAll(pattern)) {
    labels[match[1]] = match[2]
      .replace(/\\n/g, '\\n')
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\')
  }
  return labels
}

export function parsePrometheusMetrics(text: string): PrometheusSample[] {
  const samples: PrometheusSample[] = []
  const pattern = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|NaN|[+-]?Inf)(?:\s+\d+)?$/
  for (const line of text.split(/\r?\n/)) {
    if (!line || line.startsWith('#')) continue
    const match = line.match(pattern)
    if (!match) continue
    const value = match[3] === '+Inf' ? Number.POSITIVE_INFINITY
      : match[3] === '-Inf' ? Number.NEGATIVE_INFINITY
        : Number(match[3])
    if (!Number.isFinite(value)) continue
    samples.push({ name: match[1], labels: parsePrometheusLabels(match[2] ?? ''), value })
  }
  return samples
}

function sumSamples(samples: PrometheusSample[], name: string, predicate?: (sample: PrometheusSample) => boolean): number {
  return samples
    .filter((sample) => sample.name === name && (!predicate || predicate(sample)))
    .reduce((total, sample) => total + sample.value, 0)
}

export async function fetchObservability(): Promise<ObservabilitySnapshot> {
  const [health, readiness, metricsResponse, liveness] = await Promise.all([
    fetchServiceHealth(),
    fetchReadiness(),
    rawRequest('/api/metrics', { timeoutMs: 8_000, headers: { Accept: 'text/plain' } }),
    fetchHealth(),
  ])
  health.uptime_seconds = liveness.uptime_seconds
  const metricsText = await metricsResponse.text()
  const samples = parsePrometheusMetrics(metricsText)
  const requestSamples = samples.filter((sample) => sample.name === 'http_requests_total')
  const endpointRequests: Record<string, number> = {}
  const endpointErrors: Record<string, number> = {}
  for (const sample of requestSamples) {
    const endpoint = sample.labels.endpoint ?? 'unknown'
    endpointRequests[endpoint] = (endpointRequests[endpoint] ?? 0) + sample.value
    if (Number(sample.labels.status ?? 0) >= 500) {
      endpointErrors[endpoint] = (endpointErrors[endpoint] ?? 0) + sample.value
    }
  }
  const durationCount = sumSamples(samples, 'http_request_duration_seconds_count')
  const durationSum = sumSamples(samples, 'http_request_duration_seconds_sum')
  return {
    health,
    readiness,
    metrics: {
      samples,
      requests_total: requestSamples.reduce((total, sample) => total + sample.value, 0),
      errors_total: Object.values(endpointErrors).reduce((total, value) => total + value, 0),
      avg_latency_ms: durationCount ? (durationSum / durationCount) * 1000 : 0,
      rate_limit_total: sumSamples(samples, 'rate_limit_events_total'),
      llm_requests_total: sumSamples(samples, 'llm_requests_total'),
      llm_errors_total: sumSamples(samples, 'llm_requests_total', (sample) => sample.labels.outcome !== 'success'),
      llm_input_tokens: sumSamples(samples, 'llm_tokens_total', (sample) => sample.labels.direction === 'input'),
      llm_output_tokens: sumSamples(samples, 'llm_tokens_total', (sample) => sample.labels.direction === 'output'),
      llm_cost_yuan: sumSamples(samples, 'llm_cost_yuan_total'),
      rag_hit_ratio: sumSamples(samples, 'rag_queries_total') > 0
        ? (samples.find((sample) => sample.name === 'rag_hit_ratio')?.value ?? 0)
        : null,
      rag_queries_total: sumSamples(samples, 'rag_queries_total'),
      rag_hits_total: sumSamples(samples, 'rag_hits_total'),
      feedback_total: sumSamples(samples, 'feedback_events_total'),
      endpoint_requests: endpointRequests,
      endpoint_errors: endpointErrors,
    },
  }
}

export function fetchSession(sessionId: string): Promise<SessionDetail> {
  return requestJson<SessionDetail>(`/api/session/${encodeURIComponent(sessionId)}`)
}

/**
 * GET /api/sessions — history of the *currently authenticated* user
 * (identity carried by the auth header injected in rawRequest).
 */
export async function fetchSessions(): Promise<SessionSummary[]> {
  const data = await requestJson<{ sessions?: SessionSummary[] }>('/api/sessions')
  return data.sessions ?? []
}

/** GET /api/analytics. */
export function fetchAnalytics(): Promise<AnalyticsData> {
  return requestJson<AnalyticsData>('/api/analytics')
}

// ---------------------------------------------------------------------------
// Auth + long-term memory
// ---------------------------------------------------------------------------

/** POST /api/auth/login — username (+ optional password); first login registers. */
export function login(username: string, password?: string): Promise<LoginResult> {
  const body: Record<string, unknown> = { username }
  if (password) body.password = password
  return requestJson<LoginResult>('/api/auth/login', { method: 'POST', body, retryAfterRefresh: false, skipAuthRefresh: true })
}

/** POST /api/auth/register — explicit registration with optional display name. */
export function register(
  username: string,
  password?: string,
  displayName?: string,
): Promise<RegisterResult> {
  const body: Record<string, unknown> = { username }
  if (password) body.password = password
  if (displayName) body.display_name = displayName
  return requestJson<RegisterResult>('/api/auth/register', { method: 'POST', body, retryAfterRefresh: false, skipAuthRefresh: true })
}

/** GET /api/auth/me — resolve the identity the backend sees for the current headers. */
export function fetchMe(): Promise<MeResult> {
  return requestJson<MeResult>('/api/auth/me', { timeoutMs: 8_000 })
}

/** Use the HttpOnly rotating refresh cookie to obtain a new short-lived JWT. */
export async function refreshSession(): Promise<RefreshResult | null> {
  return refreshAccessToken()
}

/** POST /api/auth/logout - clear backend login cookies. */
export function logout(): Promise<OkResult> {
  return requestJson<OkResult>('/api/auth/logout', { method: 'POST', timeoutMs: 8_000, retryAfterRefresh: false, skipAuthRefresh: true })
}

/** GET /api/memory — the current user's long-term memories. */
export async function fetchMemories(): Promise<MemoryItem[]> {
  const data = await requestJson<{ memories?: MemoryItem[] }>('/api/memory')
  return data.memories ?? []
}

/** DELETE /api/memory/{id} — remove one of the current user's memories. */
export function deleteMemory(memoryId: string): Promise<DeleteMemoryResult> {
  return requestJson<DeleteMemoryResult>(`/api/memory/${encodeURIComponent(memoryId)}`, {
    method: 'DELETE',
  })
}
