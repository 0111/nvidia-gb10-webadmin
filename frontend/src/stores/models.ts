import { defineStore } from 'pinia'
import apiClient from '@/api/client'
import type { ModelInfoOut, ModelListResponse, ParamsResponse } from '@/types/api'

export const useModelsStore = defineStore('models', {
  state: () => ({
    generalModels: [] as ModelInfoOut[],
    embeddingModels: [] as ModelInfoOut[],
    loading: false,
    scannedAt: null as number | null,
    rescanning: false,
  }),
  actions: {
    async fetchModels(type: 'general' | 'embedding'): Promise<ModelInfoOut[]> {
      this.loading = true
      try {
        const { data } = await apiClient.get<ModelListResponse>('/api/models', { params: { type } })
        if (type === 'general') {
          this.generalModels = data.models
        } else {
          this.embeddingModels = data.models
        }
        this.scannedAt = data.scanned_at ?? null
        return data.models
      } finally {
        this.loading = false
      }
    },

    // 手动触发一次模型目录扫描（这是唯一会真正读取文件的操作）。
    async rescan(): Promise<any> {
      this.rescanning = true
      try {
        const { data } = await apiClient.post('/api/models/rescan')
        return data
      } finally {
        this.rescanning = false
      }
    },

    async fetchParams(name: string): Promise<ParamsResponse> {
      const { data } = await apiClient.get<ParamsResponse>(`/api/models/${encodeURIComponent(name)}/params`)
      return data
    },

    async loadModel(name: string, engine: 'vllm', params: Record<string, any>, hostPort?: number) {
      const { data } = await apiClient.post(`/api/models/${encodeURIComponent(name)}/load`, {
        engine,
        params,
        host_port: hostPort ?? null,
      })
      return data
    },

    async unloadModel(name: string) {
      const { data } = await apiClient.post(`/api/models/${encodeURIComponent(name)}/unload`)
      return data
    },
  },
})
