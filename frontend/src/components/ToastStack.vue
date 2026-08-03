<script setup lang="ts">
import { useUiStore } from '../stores/ui'

const ui = useUiStore()

const ICONS: Record<string, string> = { success: '✓', error: '✕', info: 'ℹ' }
</script>

<template>
  <div class="toast-stack" aria-live="polite">
    <div v-for="toast in ui.toasts" :key="toast.id" class="toast" :class="toast.kind">
      <span>{{ ICONS[toast.kind] }}</span>
      <span>{{ toast.text }}</span>
      <button type="button" title="关闭" @click="ui.dismissToast(toast.id)">×</button>
    </div>
  </div>
</template>
