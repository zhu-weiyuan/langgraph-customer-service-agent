<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { UiMessage } from '../stores/chat'
import { renderMarkdown } from '../utils/markdown'
import SourceChips from './SourceChips.vue'
import MessageFeedback from './MessageFeedback.vue'

const props = defineProps<{ messages: UiMessage[] }>()
const emit = defineEmits<{ suggest: [text: string] }>()

const scroller = ref<HTMLElement | null>(null)

const EMOTION_ICONS: Record<string, string> = {
  neutral: '😐', angry: '😠', sad: '😢', anxious: '😰', happy: '😊',
}

const INTENT_LABELS: Record<string, string> = {
  consult: '咨询', complaint: '投诉', chat: '闲聊', ending: '结束', cached: '缓存',
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '现在'
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function intentLabel(intent: string): string {
  return INTENT_LABELS[intent] ?? intent
}

watch(
  () => {
    const last = props.messages.length > 0 ? props.messages[props.messages.length - 1] : null
    return `${props.messages.length}:${last?.content.length ?? 0}`
  },
  async () => {
    await nextTick()
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

const SUGGESTIONS = [
  { icon: '◌', title: '设备连接', text: '音箱怎么连接 Wi‑Fi？' },
  { icon: '◇', title: '售后保障', text: '保修期是多久？可以退换吗？' },
  { icon: '↗', title: '订单进度', text: '我的订单什么时候能到？' },
]
</script>

<template>
  <div ref="scroller" class="chat-wrap">
    <div v-if="props.messages.length === 0" class="welcome-state">
      <div class="welcome-orb"><span>✦</span></div>
      <div class="eyebrow">ASTER SUPPORT · ONLINE</div>
      <h2>你好，有什么可以帮你？</h2>
      <p>我可以查询订单、处理售后、检索产品知识，并根据你的问题给出清晰的解决方案。</p>
      <div class="suggestion-grid">
        <button
          v-for="item in SUGGESTIONS"
          :key="item.title"
          class="suggestion"
          type="button"
          @click="emit('suggest', item.text)"
        >
          <span>{{ item.icon }}</span>
          <div><strong>{{ item.title }}</strong><small>{{ item.text }}</small></div>
        </button>
      </div>
    </div>

    <div
      v-for="message in props.messages"
      :key="message.id"
      class="message-shell"
      :class="message.role"
    >
      <div v-if="message.role === 'assistant'" class="message-avatar assistant-avatar">✦</div>
      <div class="message-stack">
        <div class="message-meta">
          <strong>{{ message.role === 'user' ? '你' : 'Aster 客服 Agent' }}</strong>
          <span>{{ formatTime(message.createdAt) }}</span>
        </div>

        <div class="message" :class="[message.role, { error: message.error }]">
          <!-- pending: typing dots / progress hint -->
          <span v-if="message.role === 'assistant' && !message.content && message.streaming" class="progress-hint">
            <span class="typing-dots"><i></i><i></i><i></i></span>
            <template v-if="message.progress === 'analyzing'">正在分析问题…</template>
          </span>

          <!-- assistant: sanitized markdown with typing cursor while streaming -->
          <div
            v-else-if="message.role === 'assistant'"
            class="md-body"
            :class="{ 'typing-cursor': message.streaming }"
            v-html="renderMarkdown(message.content)"
          ></div>

          <!-- user: plain text -->
          <template v-else>{{ message.content }}</template>
        </div>

        <template v-if="message.role === 'assistant' && !message.streaming && !message.error">
          <div v-if="message.intent || message.emotion || message.cached" class="badge-row">
            <span v-if="message.intent" class="badge intent">{{ intentLabel(message.intent) }}</span>
            <span
              v-if="message.emotion"
              class="badge"
              :class="`emotion-${message.emotion}`"
            >{{ EMOTION_ICONS[message.emotion] ?? '😐' }} {{ message.emotion }}<template v-if="message.emotionIntensity"> · {{ message.emotionIntensity }}/5</template></span>
            <span v-if="message.cached" class="badge cached">缓存命中</span>
          </div>

          <SourceChips v-if="message.sources?.length" :sources="message.sources" />
          <MessageFeedback v-if="message.content" :message="message" />
        </template>
      </div>
      <div v-if="message.role === 'user'" class="message-avatar user-avatar">你</div>
    </div>
  </div>
</template>
