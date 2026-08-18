<script setup lang="ts">
import { computed } from 'vue'
import type { MemoryItem } from '../api/client'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'

const chat = useChatStore()
const ui = useUiStore()

interface MemoryGroup {
  kind: string
  label: string
  icon: string
  items: MemoryItem[]
}

// Backend VALID_KINDS = fact | preference | issue.
const KIND_META: Record<string, { label: string; icon: string; order: number }> = {
  preference: { label: '偏好', icon: '⭐', order: 0 },
  issue: { label: '历史问题', icon: '🛠', order: 1 },
  fact: { label: '身份', icon: '🪪', order: 2 },
}

const groups = computed<MemoryGroup[]>(() => {
  const buckets = new Map<string, MemoryItem[]>()
  for (const item of chat.memories) {
    const kind = KIND_META[item.kind] ? item.kind : 'fact'
    const list = buckets.get(kind) ?? []
    list.push(item)
    buckets.set(kind, list)
  }
  return [...buckets.entries()]
    .map(([kind, items]) => ({
      kind,
      label: KIND_META[kind]?.label ?? kind,
      icon: KIND_META[kind]?.icon ?? '•',
      items: items.slice().sort((a, b) => b.importance - a.importance),
    }))
    .sort((a, b) => (KIND_META[a.kind]?.order ?? 9) - (KIND_META[b.kind]?.order ?? 9))
})

function importancePct(item: MemoryItem): number {
  return Math.round(Math.max(0, Math.min(1, item.importance)) * 100)
}

function close() {
  ui.memoryOpen = false
}
</script>

<template>
  <div class="memory-overlay" @click.self="close">
    <aside class="memory-drawer glass-panel">
      <header class="memory-header">
        <div>
          <div class="eyebrow">LONG-TERM MEMORY</div>
          <h2>我的长期记忆</h2>
        </div>
        <div class="memory-header-actions">
          <button class="icon-button" title="刷新" @click="chat.reloadMemories()">↻</button>
          <button class="icon-button" title="关闭" @click="close">✕</button>
        </div>
      </header>

      <p class="memory-intro">
        这些是客服助手记住的关于你的信息，登录后会跨会话保留。你可以删除任意一条，
        助手将不再据此为你服务。
      </p>

      <div v-if="chat.memoriesError" class="memory-load-error" role="alert">
        <strong>长期记忆刷新失败</strong>
        <p>{{ chat.memoriesError }}</p>
        <small v-if="chat.memories.length">已保留上次成功加载的 {{ chat.memories.length }} 条记忆。</small>
      </div>

      <div v-if="chat.memoriesLoading && !chat.memories.length" class="memory-empty">
        <div class="orb">◌</div>
        <p>正在加载记忆…</p>
      </div>

      <div v-else-if="!chat.memories.length && !chat.memoriesError" class="memory-empty">
        <div class="orb">◌</div>
        <p>还没有任何长期记忆</p>
        <small>多和助手聊聊，它会记住你的偏好、身份与历史问题。</small>
      </div>

      <div v-else-if="chat.memories.length" class="memory-groups">
        <p v-if="chat.memoriesLoading" class="memory-refreshing" aria-live="polite">正在刷新记忆…</p>
        <section v-for="group in groups" :key="group.kind" class="memory-group">
          <div class="memory-group-heading">
            <span class="memory-group-icon">{{ group.icon }}</span>
            <h3>{{ group.label }}</h3>
            <span class="memory-group-count">{{ group.items.length }}</span>
          </div>

          <div class="memory-item" v-for="item in group.items" :key="item.id">
            <div class="memory-item-body">
              <p>{{ item.content }}</p>
              <div class="memory-item-meta">
                <span class="memory-importance" :title="`重要度 ${importancePct(item)}%`">
                  <i :style="{ width: importancePct(item) + '%' }"></i>
                </span>
                <span class="memory-importance-label">重要度 {{ importancePct(item) }}%</span>
              </div>
            </div>
            <button class="memory-delete" title="删除这条记忆" @click="chat.removeMemory(item.id)">
              🗑
            </button>
          </div>
        </section>
      </div>
    </aside>
  </div>
</template>
