<script setup lang="ts">
import type { SessionSummary } from '../api/client'

const props = defineProps<{
  sessions: SessionSummary[]
  activeSessionId: string
  search: string
  available: boolean
}>()

const emit = defineEmits<{
  select: [sessionId: string]
  newSession: []
  updateSearch: [value: string]
}>()

function formatTime(value?: string): string {
  if (!value) return '新会话'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '新会话'
  const diffMs = Date.now() - date.getTime()
  if (diffMs < 60_000) return '刚刚'
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`
  if (diffMs < 86_400_000) return `${Math.floor(diffMs / 3_600_000)} 小时前`
  return `${date.getMonth() + 1}/${date.getDate()}`
}
</script>

<template>
  <aside class="sidebar">
    <button class="new-chat" @click="emit('newSession')">
      <span class="new-chat-plus">+</span>
      新建咨询
      <span class="new-chat-shortcut">⌘ K</span>
    </button>

    <div class="sidebar-section-label">会话中心</div>
    <label class="search-box">
      <span>⌕</span>
      <input
        :value="props.search"
        placeholder="搜索会话..."
        @input="emit('updateSearch', ($event.target as HTMLInputElement).value)"
      />
    </label>

    <div class="session-list">
      <button
        v-for="session in props.sessions"
        :key="session.session_id"
        class="session-item"
        :class="{ active: session.session_id === props.activeSessionId }"
        @click="emit('select', session.session_id)"
      >
        <div class="session-item-top">
          <span class="session-person">客户咨询</span>
          <span class="session-time">{{ formatTime(session.last_activity) }}</span>
        </div>
        <strong>{{ session.preview || '等待客户提问' }}</strong>
        <div class="session-item-bottom">
          <span class="session-count">{{ session.message_count }} 条对话</span>
          <span v-if="session.intents?.[0]" class="intent-pill">{{ session.intents[0] }}</span>
        </div>
      </button>

      <div v-if="!props.sessions.length" class="empty-sessions">
        <span>◌</span>
        <template v-if="props.available">
          <p>暂时没有历史会话</p>
          <small>发起一段对话后会显示在这里</small>
        </template>
        <template v-else>
          <p>会话目录暂不可用</p>
          <small>后端上线 /api/sessions 后自动恢复</small>
        </template>
      </div>
    </div>

    <div class="sidebar-footer">
      <div class="sidebar-footer-icon">⌘</div>
      <div><strong>知识库已就绪</strong><small>Hybrid RAG 检索中</small></div>
      <span class="status-dot"></span>
    </div>
  </aside>
</template>
