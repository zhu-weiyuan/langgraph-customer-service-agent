<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../stores/ui'
import { useChatStore } from '../stores/chat'

const ui = useUiStore()
const chat = useChatStore()

const healthClass = computed(() => {
  if (ui.healthOk === null) return 'unknown'
  return ui.healthOk ? '' : 'down'
})

const healthLabel = computed(() => {
  if (ui.healthOk === null) return '检测中…'
  return ui.healthOk ? '服务运行正常' : '服务不可用'
})

const shortSession = computed(() => {
  const id = chat.sessionId
  return id.length > 18 ? `${id.slice(0, 15)}…` : id
})
</script>

<template>
  <div class="status-cluster">
    <span class="status-pill" :title="ui.healthVersion ? `版本 ${ui.healthVersion} · 已运行 ${ui.uptimeSeconds}s` : healthLabel">
      <span class="status-dot" :class="healthClass"></span>
      <span class="status-label">{{ healthLabel }}</span>
    </span>
    <span class="session-pill" :title="chat.sessionId">会话 {{ shortSession }}</span>
    <button
      class="icon-button"
      :title="ui.theme === 'dark' ? '切换为亮色模式' : '切换为暗色模式'"
      @click="ui.toggleTheme()"
    >{{ ui.theme === 'dark' ? '☀️' : '🌙' }}</button>
  </div>
</template>
