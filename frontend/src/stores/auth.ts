import { defineStore } from 'pinia'
import apiClient, { configureApiClient } from '@/api/client'
import type { LoginRequest, LoginResponse } from '@/types/api'

const STORAGE_KEY = 'gb10_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(STORAGE_KEY) as string | null,
    username: '' as string,
  }),
  getters: {
    isAuthenticated: (state) => !!state.token,
  },
  actions: {
    async login(payload: LoginRequest): Promise<void> {
      const { data } = await apiClient.post<LoginResponse>('/api/auth/login', payload)
      this.token = data.access_token
      this.username = payload.username
      localStorage.setItem(STORAGE_KEY, data.access_token)
    },
    logout(): void {
      this.token = null
      this.username = ''
      localStorage.removeItem(STORAGE_KEY)
    },
  },
})

// Wire the axios client to this store's token + a logout-on-401 callback.
// Called once from main.ts after pinia is installed.
export function bindAuthToApiClient(): void {
  configureApiClient({
    getToken: () => localStorage.getItem(STORAGE_KEY),
    onUnauthorized: () => {
      localStorage.removeItem(STORAGE_KEY)
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    },
  })
}
