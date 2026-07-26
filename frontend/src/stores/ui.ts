import { defineStore } from 'pinia'
import { fetchHealth } from '../api/client'

export type Theme = 'dark' | 'light'

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'info'
  text: string
}

let toastSeq = 0

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: 'dark' as Theme,
    analyticsOpen: true,
    sidebarOpen: false,
    toasts: [] as Toast[],
    healthOk: null as boolean | null,
    healthVersion: '' as string,
    uptimeSeconds: 0,
    _healthTimer: 0 as number,
  }),
  actions: {
    applyTheme(theme: Theme) {
      this.theme = theme
      document.documentElement.dataset.theme = theme
    },
    toggleTheme() {
      this.applyTheme(this.theme === 'dark' ? 'light' : 'dark')
    },
    toggleAnalytics() {
      this.analyticsOpen = !this.analyticsOpen
    },
    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen
    },
    toast(kind: Toast['kind'], text: string, durationMs = 3200) {
      const id = ++toastSeq
      this.toasts.push({ id, kind, text })
      window.setTimeout(() => this.dismissToast(id), durationMs)
    },
    dismissToast(id: number) {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    },
    async pollHealth() {
      try {
        const health = await fetchHealth()
        this.healthOk = health.status === 'ok'
        this.healthVersion = health.version ?? ''
        this.uptimeSeconds = health.uptime_seconds ?? 0
      } catch {
        this.healthOk = false
      }
    },
    startHealthPolling(intervalMs = 30_000) {
      this.applyTheme(this.theme)
      void this.pollHealth()
      window.clearInterval(this._healthTimer)
      this._healthTimer = window.setInterval(() => void this.pollHealth(), intervalMs)
    },
    stopHealthPolling() {
      window.clearInterval(this._healthTimer)
      this._healthTimer = 0
    },
  },
})
