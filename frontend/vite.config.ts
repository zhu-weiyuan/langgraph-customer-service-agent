import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  // These values are read only by the local Vite dev-server process. They are
  // never exposed as VITE_* browser variables or bundled into frontend assets.
  const env = loadEnv(mode, '..', '')
  const apiKey = env.API_KEYS?.split(',')[0]?.trim()
  const backend = env.BACKEND_URL?.trim() || 'http://127.0.0.1:7862'

  const proxyEntry = {
    target: backend,
    changeOrigin: true,
    ...(apiKey ? { headers: { 'X-API-Key': apiKey } } : {}),
  }

  return {
    plugins: [vue()],
    server: {
      port: 5173,
      proxy: {
        '/api': proxyEntry,
        '/healthz': proxyEntry,
      },
    },
  }
})
