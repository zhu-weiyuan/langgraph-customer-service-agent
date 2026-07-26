<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import SessionSidebar from './components/SessionSidebar.vue'
import MessageList from './components/MessageList.vue'
import ChatInput from './components/ChatInput.vue'
import AnalyticsPanel from './components/AnalyticsPanel.vue'
import StatusBar from './components/StatusBar.vue'
import ToastStack from './components/ToastStack.vue'
import { useChatStore } from './stores/chat'
import { useUiStore } from './stores/ui'

const store = useChatStore()
const ui = useUiStore()

const chatInput = ref<InstanceType<typeof ChatInput> | null>(null)
const messageCount = computed(() => store.messages.length)

function onSuggest(text: string) {
  chatInput.value?.setText(text)
}

function onSelectSession(sessionId: string) {
  ui.sidebarOpen = false
  void store.selectSession(sessionId)
}

function onNewSession() {
  ui.sidebarOpen = false
  store.newSession()
}

function onKeydown(event: KeyboardEvent) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    onNewSession()
  }
}

onMounted(() => {
  ui.startHealthPolling()
  window.addEventListener('keydown', onKeydown)
  void store.bootstrap()
})

onBeforeUnmount(() => {
  ui.stopHealthPolling()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="app-shell">
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>

    <header class="topbar">
      <div class="product-mark">
        <button class="icon-button sidebar-toggle" title="会话列表" @click="ui.toggleSidebar()">☰</button>
        <div class="logo-orb"><span>✦</span></div>
        <div>
          <div class="product-name">Aster <span>Support</span></div>
          <div class="product-subtitle">AI CUSTOMER OPERATIONS</div>
        </div>
      </div>

      <div class="topbar-actions">
        <StatusBar />
        <button
          class="icon-button"
          :class="{ active: ui.analyticsOpen }"
          title="切换洞察面板"
          @click="ui.toggleAnalytics()"
        >📊 洞察</button>
      </div>
    </header>

    <div class="workspace" :class="{ 'no-analytics': !ui.analyticsOpen }">
      <section class="left-rail glass-panel" :class="{ open: ui.sidebarOpen }">
        <SessionSidebar
          :sessions="store.filteredSessions"
          :active-session-id="store.sessionId"
          :search="store.sessionSearch"
          :available="store.sessionsAvailable"
          @select="onSelectSession"
          @new-session="onNewSession"
          @update-search="store.sessionSearch = $event"
        />
      </section>

      <main class="conversation glass-panel">
        <div class="conversation-header">
          <div class="conversation-title">
            <div class="title-icon">◌</div>
            <div>
              <div class="eyebrow">当前会话</div>
              <h1>售后支持对话</h1>
            </div>
          </div>
          <div class="conversation-actions">
            <span class="message-count">{{ messageCount }} 条消息</span>
          </div>
        </div>

        <MessageList :messages="store.messages" @suggest="onSuggest" />

        <ChatInput
          ref="chatInput"
          :loading="store.loading"
          :can-cancel="store.canCancel"
          @send="store.send"
          @cancel="store.cancelStreaming()"
        />
      </main>

      <section v-if="ui.analyticsOpen" class="right-rail glass-panel">
        <AnalyticsPanel
          :analytics="store.analytics"
          :available="store.analyticsAvailable"
          @refresh="store.reloadAnalytics()"
        />
      </section>
    </div>

    <ToastStack />
  </div>
</template>
