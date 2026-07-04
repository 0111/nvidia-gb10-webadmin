import { defineStore } from 'pinia'
import type { WsEnvelope } from '@/types/api'

type Callback = (data: any) => void

const RECONNECT_DELAY_MS = 3000

// Single shared WebSocket connection. Pages subscribe to topics via
// `subscribe(topic, cb)`; the connection itself is opened once (call
// `connect()` after login) and reused across route navigations.
export const useWsStore = defineStore('ws', {
  state: () => ({
    socket: null as WebSocket | null,
    connected: false,
    reconnectTimer: null as ReturnType<typeof setTimeout> | null,
    listeners: {} as Record<string, Set<Callback>>,
    topics: ['metrics', 'load_progress'] as string[],
    // Live-log streaming target (组件日志 / 模型配置 页)，需在每次(重)连接后
    // 重新告知后端，故存在 store 里。
    logTarget: null as { component: string; lines: number } | null,
  }),
  actions: {
    connect(token: string): void {
      this.disconnect()

      // Same default-backend-port reasoning as api/client.ts: `vite
      // preview` serves the frontend on its own port (4173) with no proxy,
      // so `window.location.host` would point the socket at the frontend's
      // own static server (which has no /ws route) instead of the backend.
      const DEFAULT_BACKEND_PORT = 8000
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const base =
        (import.meta as any).env?.VITE_WS_BASE ||
        `${protocol}://${window.location.hostname}:${DEFAULT_BACKEND_PORT}`
      const url = `${base}/ws?token=${encodeURIComponent(token)}&topics=${encodeURIComponent(this.topics.join(','))}`

      const ws = new WebSocket(url)
      this.socket = ws

      ws.onopen = () => {
        this.connected = true
        // Re-assert the log target after a (re)connect so live log streaming
        // survives socket drops without the page having to re-request it.
        if (this.logTarget) {
          this.send({ action: 'set_log_target', ...this.logTarget })
        }
      }

      ws.onmessage = (event) => {
        try {
          const envelope: WsEnvelope = JSON.parse(event.data)
          const subs = this.listeners[envelope.topic]
          if (subs) {
            subs.forEach((cb) => cb(envelope.data))
          }
        } catch {
          // ignore malformed frames
        }
      }

      ws.onclose = () => {
        this.connected = false
        this.socket = null
        // Simple fixed-delay retry — sufficient for an internal tool, no
        // exponential backoff required per spec.
        this.reconnectTimer = setTimeout(() => {
          const currentToken = localStorage.getItem('gb10_token')
          if (currentToken) this.connect(currentToken)
        }, RECONNECT_DELAY_MS)
      }

      ws.onerror = () => {
        // onclose will fire right after; reconnection handled there.
      }
    },

    disconnect(): void {
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      if (this.socket) {
        this.socket.onclose = null
        this.socket.close()
        this.socket = null
      }
      this.connected = false
    },

    subscribe(topic: string, cb: Callback): () => void {
      if (!this.listeners[topic]) {
        this.listeners[topic] = new Set()
      }
      this.listeners[topic].add(cb)

      // Ask the server to add this topic dynamically if we haven't already.
      if (!this.topics.includes(topic)) {
        this.topics.push(topic)
        this.sendSubscribe(this.topics)
      }

      return () => {
        this.listeners[topic]?.delete(cb)
      }
    },

    sendSubscribe(topics: string[]): void {
      this.send({ action: 'subscribe', topics })
    },

    // Generic send (no-op if socket not open; the caller's state is re-asserted
    // on reconnect for things that need it, e.g. logTarget).
    send(payload: Record<string, any>): void {
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify(payload))
      }
    },

    // Start/stop live log streaming of a component over the WS (replaces REST
    // log polling). Pass null to stop. Re-asserted automatically on reconnect.
    setLogTarget(component: string | null, lines = 200): void {
      this.logTarget = component ? { component, lines } : null
      this.send({ action: 'set_log_target', component, lines })
    },
  },
})
