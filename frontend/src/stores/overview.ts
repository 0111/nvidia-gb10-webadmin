import { defineStore } from 'pinia'
import apiClient from '@/api/client'
import type { OverviewResponse } from '@/types/api'

export const useOverviewStore = defineStore('overview', {
  state: () => ({
    data: null as OverviewResponse | null,
    loading: false,
    error: '' as string,
  }),
  actions: {
    async fetchOverview(): Promise<void> {
      this.loading = true
      this.error = ''
      try {
        const { data } = await apiClient.get<OverviewResponse>('/api/overview')
        this.data = data
      } catch (err: any) {
        this.error = err?.response?.data?.detail || '加载总览数据失败'
      } finally {
        this.loading = false
      }
    },
    // Called when a fresh overview snapshot arrives over the WebSocket
    // 'overview' topic (pushed every 10s by the backend MetricsCollector),
    // so the page no longer needs its own REST poll loop.
    setData(data: OverviewResponse): void {
      this.data = data
      this.error = ''
    },
  },
})
