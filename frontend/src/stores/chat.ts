import { defineStore } from 'pinia'
import {
  ApiError,
  deleteMemory,
  fetchAnalytics,
  fetchMemories,
  fetchObservability,
  fetchSession,
  fetchSessions,
  sendChat,
  streamChat,
  submitFeedback,
  submitRating,
  submitReaction,
  type AnalyticsData,
  type ChatResult,
  type MemoryItem,
  type ObservabilitySnapshot,
  type RagSource,
  type Role,
  type SessionSummary,
  type StreamMeta,
} from '../api/client'
import { useUiStore } from './ui'

export interface FeedbackState {
  reaction: '' | '👍' | '👎'
  stars: number
  commentOpen: boolean
  comment: string
  commentSubmitted: boolean
}

export interface UiMessage {
  id: string
  role: Role
  content: string
  createdAt: string
  streaming: boolean
  progress: string
  error: boolean
  replyType?: string
  intent?: string
  emotion?: string
  emotionIntensity?: number
  cached?: boolean
  sources?: RagSource[]
  feedback?: FeedbackState
}

function makeId(prefix: string): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function newFeedback(): FeedbackState {
  return { reaction: '', stars: 0, commentOpen: false, comment: '', commentSubmitted: false }
}

function makeMessage(role: Role, content: string): UiMessage {
  return {
    id: makeId('msg'),
    role,
    content,
    createdAt: new Date().toISOString(),
    streaming: false,
    progress: '',
    error: false,
    ...(role === 'assistant' ? { feedback: newFeedback() } : {}),
  }
}

function normalizeSources(value: unknown): RagSource[] | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined
  return value
    .map((item): RagSource | null => {
      if (typeof item === 'string') return { title: item }
      if (item && typeof item === 'object') return item as RagSource
      return null
    })
    .filter((item): item is RagSource => item !== null)
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: makeId('web'),
    sessions: [] as SessionSummary[],
    sessionsAvailable: true,
    sessionSearch: '',
    messages: [] as UiMessage[],
    analytics: null as AnalyticsData | null,
    analyticsAvailable: true,
    observability: null as ObservabilitySnapshot | null,
    observabilityLoading: false,
    memories: [] as MemoryItem[],
    memoriesLoading: false,
    loading: false,
    _abort: null as AbortController | null,
  }),
  getters: {
    filteredSessions(state): SessionSummary[] {
      const keyword = state.sessionSearch.trim().toLowerCase()
      if (!keyword) return state.sessions
      return state.sessions.filter((session) =>
        [session.title ?? '', session.session_id]
          .join(' ')
          .toLowerCase()
          .includes(keyword),
      )
    },
    canCancel(state): boolean {
      return state.loading && state._abort !== null
    },
  },
  actions: {
    /**
     * Load everything scoped to the current logged-in user, then open their
     * most recent history session (so the same user "keeps their memory").
     */
    async bootstrap() {
      // Sessions are the source of truth for the first screen.  Load them
      // before optional analytics/memory calls so a slow or unavailable
      // auxiliary panel cannot race history selection.
      await this.reloadSessions()
      await Promise.all([this.reloadAnalytics(), this.reloadMemories(), this.reloadObservability()])
      await this.openMostRecentSession()
    },

    async reloadSessions() {
      try {
        this.sessions = await fetchSessions()
        this.sessionsAvailable = true
      } catch (err) {
        this.sessionsAvailable = false
        this.sessions = []
        const ui = useUiStore()
        const message = err instanceof Error ? err.message : '未知错误'
        console.error('Failed to load sessions', err)
        ui.toast('error', `加载历史会话失败：${message}`)
      }
    },

    /** After (re)loading history, open the most recent session if one exists. */
    async openMostRecentSession() {
      const latest = this.sessions[0]
      if (latest && latest.session_id !== this.sessionId) {
        await this.selectSession(latest.session_id)
      }
    },

    /** Wipe per-user state and start a fresh anonymous session (used on logout). */
    resetForUser() {
      this.cancelStreaming()
      this.sessionId = makeId('web')
      this.messages = []
      this.sessions = []
      this.memories = []
      this.analytics = null
      this.observability = null
      this.sessionSearch = ''
    },

    async reloadAnalytics() {
      try {
        this.analytics = await fetchAnalytics()
        this.analyticsAvailable = true
      } catch {
        // /api/analytics only exists in the legacy backend for now.
        this.analytics = null
        this.analyticsAvailable = false
      }
    },

    async reloadObservability() {
      this.observabilityLoading = true
      try {
        this.observability = await fetchObservability()
      } catch (err) {
        this.observability = null
        console.error('Failed to load observability', err)
      } finally {
        this.observabilityLoading = false
      }
    },

    async reloadMemories() {
      this.memoriesLoading = true
      try {
        this.memories = await fetchMemories()
      } catch {
        this.memories = []
      } finally {
        this.memoriesLoading = false
      }
    },

    async removeMemory(memoryId: string) {
      const ui = useUiStore()
      const previous = this.memories
      this.memories = this.memories.filter((m) => m.id !== memoryId)
      try {
        const result = await deleteMemory(memoryId)
        if (result.error) throw new Error(result.error)
        ui.toast('success', '已删除该条记忆')
      } catch (err) {
        this.memories = previous
        ui.toast('error', `删除失败：${err instanceof Error ? err.message : '未知错误'}`)
      }
    },

    newSession() {
      this.cancelStreaming()
      this.sessionId = makeId('web')
      this.messages = []
    },

    async selectSession(sessionId: string) {
      if (sessionId === this.sessionId) return
      this.cancelStreaming()
      this.sessionId = sessionId
      try {
        const detail = await fetchSession(sessionId)
        this.messages = (detail.messages ?? []).map((item) => makeMessage(item.role, item.content))
      } catch (err) {
        this.messages = []
        useUiStore().toast('error', `加载会话失败：${err instanceof Error ? err.message : '未知错误'}`)
      }
    },

    cancelStreaming() {
      this._abort?.abort()
      this._abort = null
    },

    _applyMeta(message: UiMessage, meta: StreamMeta | ChatResult) {
      if (typeof meta.intent === 'string' && meta.intent) message.intent = meta.intent
      if (typeof meta.emotion === 'string' && meta.emotion) message.emotion = meta.emotion
      if (typeof meta.emotion_intensity === 'number') message.emotionIntensity = meta.emotion_intensity
      if (typeof meta.session_id === 'string' && meta.session_id) this.sessionId = meta.session_id
      const replyType = (meta as StreamMeta).reply_type
      if (typeof replyType === 'string' && replyType) message.replyType = replyType
      if ('cached' in meta && typeof meta.cached === 'boolean') message.cached = meta.cached
      const sources = normalizeSources((meta as Record<string, unknown>).sources)
      if (sources) message.sources = sources
    },

    async _sendBuffered(text: string, assistant: UiMessage) {
      const data = await sendChat(text, this.sessionId)
      if (data.error) {
        assistant.content = data.error
        assistant.error = true
        return
      }
      assistant.content = (data.replies ?? [])
        .map((reply) => reply.content)
        .filter(Boolean)
        .join('\n\n') || '（暂无响应）'
      const replies = data.replies ?? []
      const lastType = replies.length > 0 ? replies[replies.length - 1].type : ''
      if (lastType) assistant.replyType = lastType
      this._applyMeta(assistant, data)
    },

    async send(rawMessage: string) {
      const text = rawMessage.trim()
      if (!text || this.loading) return

      const ui = useUiStore()
      this.loading = true
      this.messages.push(makeMessage('user', text))
      const draft = makeMessage('assistant', '')
      draft.streaming = true
      this.messages.push(draft)
      // Re-read through the store so mutations hit the reactive proxy.
      const assistant = this.messages[this.messages.length - 1]

      const controller = new AbortController()
      this._abort = controller

      try {
        const streamState: { error: string | null } = { error: null }
        const completed = await streamChat(
          text,
          this.sessionId,
          {
            onToken: (token) => {
              assistant.progress = ''
              assistant.content += token
            },
            onProgress: (stage) => {
              if (!assistant.content) assistant.progress = stage
            },
            onDone: (meta) => this._applyMeta(assistant, meta),
            onError: (message) => {
              streamState.error = message
            },
          },
          controller.signal,
        )

        if (streamState.error !== null) {
          if (assistant.content) {
            // Keep partial answer, surface the error non-destructively.
            ui.toast('error', `回复中断：${streamState.error}`)
          } else {
            assistant.content = streamState.error
            assistant.error = true
          }
        } else if (!completed && controller.signal.aborted) {
          // User cancel: keep whatever partial text arrived.
          if (!assistant.content) {
            assistant.content = '（已取消）'
            assistant.error = true
          }
        } else if (!assistant.content.trim()) {
          // Stream ended without content — fall back to buffered mode.
          await this._sendBuffered(text, assistant)
        }
      } catch (err) {
        // SSE transport failed entirely; fall back to the non-streaming API.
        if (err instanceof ApiError && err.kind === 'abort') {
          if (!assistant.content) {
            assistant.content = '（已取消）'
            assistant.error = true
          }
        } else if (assistant.content.trim()) {
          // A transport failure after tokens arrived must not trigger a second
          // LLM request: that would duplicate the answer and waste tokens.
          // Keep the partial answer visible and mark it as interrupted.
          assistant.error = true
          ui.toast('error', `Stream interrupted: ${err instanceof Error ? err.message : 'stream connection interrupted'}`)
        } else {
          try {
            // Only use the buffered endpoint when the stream failed before
            // producing any assistant content (e.g. an old backend/proxy).
            await this._sendBuffered(text, assistant)
          } catch (fallbackErr) {
            assistant.content =
              fallbackErr instanceof Error ? `Request failed: ${fallbackErr.message}` : 'Request failed: unable to connect to the support service'
            assistant.error = true
          }
        }
      } finally {
        assistant.streaming = false
        assistant.progress = ''
        if (this._abort === controller) this._abort = null
        this.loading = false
      }

      // The server has finished persisting before the SSE done frame. Await the
      // follow-up reads so a user who refreshes immediately after a reply sees
      // the newly created session and its durable history.
      await this.reloadSessions()
      await Promise.all([this.reloadAnalytics(), this.reloadMemories(), this.reloadObservability()])
    },

    // -----------------------------------------------------------------------
    // Feedback loop (self-improvement pipeline)
    // -----------------------------------------------------------------------

    _findQueryFor(message: UiMessage): string {
      const idx = this.messages.findIndex((m) => m.id === message.id)
      for (let i = idx - 1; i >= 0; i--) {
        const candidate = this.messages[i]
        if (candidate.role === 'user') return candidate.content
      }
      return ''
    },

    async react(messageId: string, emoji: '👍' | '👎') {
      const message = this.messages.find((m) => m.id === messageId)
      if (!message?.feedback) return
      const ui = useUiStore()
      const previous = message.feedback.reaction
      const next = previous === emoji ? '' : emoji
      message.feedback.reaction = next
      if (next === '👎') message.feedback.commentOpen = true

      try {
        await submitReaction(this.sessionId, message.id, emoji, next === emoji)
        ui.toast('success', next ? '已记录你的反馈' : '已取消反馈')
      } catch (err) {
        message.feedback.reaction = previous
        ui.toast('error', `反馈提交失败：${err instanceof Error ? err.message : '未知错误'}`)
      }
    },

    async rate(messageId: string, stars: number) {
      const message = this.messages.find((m) => m.id === messageId)
      if (!message?.feedback) return
      const ui = useUiStore()
      const previous = message.feedback.stars
      message.feedback.stars = stars
      if (stars > 0 && stars <= 2) message.feedback.commentOpen = true
      const messageIndex = this.messages.findIndex((m) => m.id === messageId)

      try {
        await submitRating(this.sessionId, messageIndex, stars)
        ui.toast('success', `感谢评分：${stars} 星`)
      } catch (err) {
        message.feedback.stars = previous
        ui.toast('error', `评分提交失败：${err instanceof Error ? err.message : '未知错误'}`)
      }
    },

    async submitComment(messageId: string) {
      const message = this.messages.find((m) => m.id === messageId)
      if (!message?.feedback) return
      const comment = message.feedback.comment.trim()
      if (!comment) return
      const ui = useUiStore()
      message.feedback.commentSubmitted = true
      message.feedback.commentOpen = false

      try {
        const result = await submitFeedback(
          this.sessionId,
          this._findQueryFor(message),
          message.content,
          message.feedback.stars,
          comment,
        )
        if (result.error) throw new Error(result.error)
        ui.toast('success', '意见已提交，感谢你帮助改进服务')
      } catch (err) {
        message.feedback.commentSubmitted = false
        message.feedback.commentOpen = true
        ui.toast('error', `意见提交失败：${err instanceof Error ? err.message : '未知错误'}`)
      }
    },
  },
})
