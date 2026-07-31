<script setup lang="ts">
import { ref } from 'vue'
import { useUiStore } from '../stores/ui'

const emit = defineEmits<{ authenticated: [] }>()

const ui = useUiStore()

const username = ref('')
const password = ref('')
const usePassword = ref(false)
const submitting = ref(false)

async function submit() {
  if (submitting.value) return
  const name = username.value.trim()
  if (!name) {
    ui.toast('error', '请输入用户名')
    return
  }
  submitting.value = true
  try {
    const ok = await ui.login(name, usePassword.value ? password.value : undefined)
    if (ok) emit('authenticated')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-overlay">
    <div class="ambient ambient-one"></div>
    <div class="ambient ambient-two"></div>

    <form class="auth-card glass-panel" @submit.prevent="submit">
      <div class="auth-brand">
        <div class="logo-orb"><span>✦</span></div>
        <div>
          <div class="product-name">Aster <span>Support</span></div>
          <div class="product-subtitle">AI CUSTOMER OPERATIONS</div>
        </div>
      </div>

      <div class="auth-heading">
        <div class="eyebrow">欢迎使用</div>
        <h1>登录以继续</h1>
        <p>输入用户名即可登录，首次登录将自动为你创建账号并保留长期记忆。</p>
      </div>

      <label class="auth-field">
        <span>用户名</span>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          placeholder="例如 alice"
          maxlength="128"
          autofocus
        />
      </label>

      <label v-if="usePassword" class="auth-field">
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          placeholder="可选，用于保护账号"
          maxlength="256"
        />
      </label>

      <label class="auth-toggle">
        <input v-model="usePassword" type="checkbox" />
        <span>使用密码登录（可选）</span>
      </label>

      <button class="auth-submit" type="submit" :disabled="submitting">
        {{ submitting ? '登录中…' : '登录 / 注册' }}
      </button>

      <p class="auth-footnote">登录态将保存在本地，刷新页面不会掉登录。</p>
    </form>
  </div>
</template>
