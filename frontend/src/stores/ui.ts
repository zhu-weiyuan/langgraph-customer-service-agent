import { defineStore } from 'pinia'
import {
  ApiError,
  fetchHealth,
  fetchMe,
  refreshSession,
  login as apiLogin,
  register as apiRegister,
  logout as apiLogout,
  setAuthIdentity,
} from '../api/client'

export type Theme = 'dark' | 'light'

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'info'
  text: string
}

export interface AuthState {
  userId: string
  username: string
  token: string | null
  isLoggedIn: boolean
}

let toastSeq = 0

// Browser-persistent auth keys. Prefer localStorage, but also mirror to
// first-party cookies because the Codex in-app browser can run pages with
// localStorage unavailable. Without this fallback a hard refresh loses the
// `zwy` identity, so the page loads an empty/anonymous history even though
// PostgreSQL still has the new messages.
const LS_USER = 'aster.auth.user_id'
const LS_NAME = 'aster.auth.username'
const COOKIE_MAX_AGE = 60 * 60 * 24 * 30

function cookieGet(key: string): string | null {
  try {
    const prefix = `${encodeURIComponent(key)}=`
    const part = document.cookie
      .split(';')
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix))
    return part ? decodeURIComponent(part.slice(prefix.length)) : null
  } catch {
    return null
  }
}

function cookieSet(key: string, value: string): void {
  try {
    document.cookie = `${encodeURIComponent(key)}=${encodeURIComponent(value)}; Max-Age=${COOKIE_MAX_AGE}; Path=/; SameSite=Lax`
  } catch {
    /* cookies unavailable — stay in-memory */
  }
}

function cookieRemove(key: string): void {
  try {
    document.cookie = `${encodeURIComponent(key)}=; Max-Age=0; Path=/; SameSite=Lax`
  } catch {
    /* ignore */
  }
}

function lsGet(key: string): string | null {
  try {
    const value = window.localStorage?.getItem(key)
    if (value) return value
  } catch {
    /* fall back to cookie mirror */
  }
  return cookieGet(key)
}

function lsSet(key: string, value: string): void {
  try {
    window.localStorage?.setItem(key, value)
  } catch {
    /* storage unavailable (private mode / sandbox) — cookie mirror below */
  }
  cookieSet(key, value)
}

function lsRemove(key: string): void {
  try {
    window.localStorage?.removeItem(key)
  } catch {
    /* ignore */
  }
  cookieRemove(key)
}

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: 'dark' as Theme,
    analyticsOpen: true,
    sidebarOpen: false,
    memoryOpen: false,
    toasts: [] as Toast[],
    healthOk: null as boolean | null,
    healthVersion: '' as string,
    uptimeSeconds: 0,
    _healthTimer: 0 as number,
    auth: { userId: '', username: '', token: null, isLoggedIn: false } as AuthState,
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
    toggleMemory() {
      this.memoryOpen = !this.memoryOpen
    },
    toast(kind: Toast['kind'], text: string, durationMs = 3200) {
      const id = ++toastSeq
      this.toasts.push({ id, kind, text })
      window.setTimeout(() => this.dismissToast(id), durationMs)
    },
    dismissToast(id: number) {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    },

    // -----------------------------------------------------------------------
    // Auth
    // -----------------------------------------------------------------------

    /** Push the current identity down to the API client's header injector. */
    _syncClientAuth() {
      if (this.auth.isLoggedIn) {
        setAuthIdentity({ userId: this.auth.userId, token: this.auth.token })
      } else {
        setAuthIdentity(null)
      }
    },

    _persistAuth() {
      lsSet(LS_USER, this.auth.userId)
      lsSet(LS_NAME, this.auth.username)
      // Access JWTs intentionally stay in memory. Page reload obtains a fresh
      // one through the HttpOnly rotating refresh cookie instead of localStorage.
    },

    _setLoggedIn(userId: string, username: string, token: string | null) {
      this.auth.userId = userId
      this.auth.username = username || userId
      this.auth.token = token
      this.auth.isLoggedIn = true
      this._syncClientAuth()
      this._persistAuth()
    },

    /**
     * Restore a persisted session on startup. Returns true when a stored
     * identity was found (caller then bootstraps sessions/memory).
     */
    restoreAuth(): boolean {
      const userId = lsGet(LS_USER)
      if (!userId) return false
      this._setLoggedIn(userId, lsGet(LS_NAME) ?? userId, null)
      return true
    },

    async login(username: string, password?: string): Promise<boolean> {
      const name = username.trim()
      if (!name) return false
      try {
        const res = await apiLogin(name, password)
        if (!res.ok) throw new ApiError(res.error ?? '登录失败')
        this._setLoggedIn(res.user_id, name, res.access_token)
        this.toast('success', res.registered ? `注册成功，欢迎 ${name}` : `欢迎回来，${name}`)
        return true
      } catch (err) {
        this.toast('error', `登录失败：${err instanceof Error ? err.message : '未知错误'}`)
        return false
      }
    },

    async register(username: string, password?: string, displayName?: string): Promise<boolean> {
      const name = username.trim()
      if (!name) return false
      try {
        const res = await apiRegister(name, password, displayName)
        if (!res.ok) throw new ApiError(res.error ?? '注册失败')
        this._setLoggedIn(res.user_id, displayName?.trim() || name, res.access_token)
        this.toast('success', res.created ? `注册成功，欢迎 ${name}` : '账号已存在，已直接登录')
        return true
      } catch (err) {
        this.toast('error', `注册失败：${err instanceof Error ? err.message : '未知错误'}`)
        return false
      }
    },

    /** Clear only browser-visible auth state; the server cookie is handled separately. */
    clearAuth() {
      this.auth = { userId: '', username: '', token: null, isLoggedIn: false }
      this.memoryOpen = false
      this.sidebarOpen = false
      setAuthIdentity(null)
      lsRemove(LS_USER)
      lsRemove(LS_NAME)
    },

    logout() {
      // Revoke the server session and clear local state immediately so no
      // user-scoped data remains visible while logout is in flight.
      void apiLogout().catch(() => undefined)
      this.clearAuth()
      this.toast('info', '???')
    },

    /**
     * Establish and verify the current browser session before user-scoped data
     * is loaded. Access JWTs are memory-only, so a page reload first exchanges
     * the HttpOnly rotating refresh cookie for a fresh access token.
     */
    async verifyMe(): Promise<boolean> {
      try {
        if (!this.auth.token) {
          const refreshed = await refreshSession()
          if (!refreshed) return false
          this.auth.userId = refreshed.user_id
          this.auth.username = this.auth.username || refreshed.user_id
          this.auth.token = refreshed.access_token
          this.auth.isLoggedIn = true
          this._syncClientAuth()
          this._persistAuth()
        }

        const me = await fetchMe()
        if (!me.authenticated || me.auth_scheme !== 'jwt') return false

        this.auth.userId = me.user_id
        this.auth.username = this.auth.username || me.user_id
        this.auth.isLoggedIn = true
        this._syncClientAuth()
        this._persistAuth()
        return true
      } catch {
        return false
      }
    },

    // -----------------------------------------------------------------------
    // Health polling
    // -----------------------------------------------------------------------
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
