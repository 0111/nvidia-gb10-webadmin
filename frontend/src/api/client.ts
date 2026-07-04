import axios, { type AxiosInstance } from 'axios'

// The frontend is served by `vite preview` (a static file server, no /api
// proxy) on a *different* port than the FastAPI backend (default 8000) —
// same-origin relative requests would 404 against the frontend's own
// static server instead of reaching the backend. Default to the backend's
// known default port on the same hostname the page was loaded from;
// override with VITE_API_BASE at build time if web_port was changed from
// the default 8000 in config/settings.yaml.
const DEFAULT_BACKEND_PORT = 8000
const API_BASE =
  (import.meta as any).env?.VITE_API_BASE ||
  `${window.location.protocol}//${window.location.hostname}:${DEFAULT_BACKEND_PORT}`

export const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE,
  // core.model_scanner caches GGUF compatibility checks, but the cache is
  // only warm after the backend's startup pre-warm task finishes (or after
  // the first real request pays that cost) — give requests more headroom
  // than the default 30s so a cold-cache /api/overview/models call doesn't
  // spuriously fail right after a restart.
  timeout: 60000,
})

// Lazily require the auth store to avoid a circular import at module-eval
// time (stores/auth.ts doesn't import this client at top level usage time).
let getToken: () => string | null = () => localStorage.getItem('gb10_token')
let onUnauthorized: () => void = () => {
  window.location.href = '/login'
}

export function configureApiClient(opts: { getToken?: () => string | null; onUnauthorized?: () => void }) {
  if (opts.getToken) getToken = opts.getToken
  if (opts.onUnauthorized) onUnauthorized = opts.onUnauthorized
}

apiClient.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers ?? {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      onUnauthorized()
    }
    return Promise.reject(error)
  },
)

export default apiClient
