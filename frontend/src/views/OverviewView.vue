<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import apiClient from '@/api/client'
import { useOverviewStore } from '@/stores/overview'
import { useModelsStore } from '@/stores/models'
import { useWebSocketTopic } from '@/composables/useWebSocket'
import type { ApiHealthResult, LoadProgressData, MetricsData } from '@/types/api'

interface ApiDirectoryEntry {
  name: string
  purpose: string
  base_url: string | null
  auth_hint: string
}

interface OverviewSnapshot {
  components?: unknown[]
  [k: string]: unknown
}

const overviewStore = useOverviewStore()
const modelsStore = useModelsStore()

const liveMetrics = ref<MetricsData | null>(null)
const unloadingModel = ref<string | null>(null)
const progressMessage = ref('')
const apiDirectory = ref<ApiDirectoryEntry[]>([])

// API 健康主动检测：按名称索引每个端点的最近一次探测结果，供表格逐行显示。
const apiHealth = ref<Record<string, ApiHealthResult>>({})
const apiHealthChecking = ref(false)
const apiHealthOverall = ref<boolean | null>(null)
const apiHealthCheckedAt = ref<string>('')

let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchApiDirectory(): Promise<void> {
  try {
    const { data } = await apiClient.get<{ entries: ApiDirectoryEntry[] }>('/api/api-directory')
    apiDirectory.value = data.entries
  } catch {
    apiDirectory.value = []
  }
}

async function runApiHealthCheck(): Promise<void> {
  apiHealthChecking.value = true
  try {
    const { data } = await apiClient.get<{
      overall_healthy: boolean
      checked_at: number
      results: ApiHealthResult[]
    }>('/api/api-directory/health-check')
    apiHealth.value = Object.fromEntries(data.results.map((r) => [r.name, r]))
    apiHealthOverall.value = data.overall_healthy
    apiHealthCheckedAt.value = new Date(data.checked_at * 1000).toLocaleTimeString()
  } catch (err: any) {
    apiHealthOverall.value = null
    progressMessage.value = err?.response?.data?.detail || 'API 健康检测失败'
  } finally {
    apiHealthChecking.value = false
  }
}

onMounted(async () => {
  // One REST call each on mount just to seed the page instantly (so we don't
  // wait up to 10s for the first WS push). After that, the backend
  // MetricsCollector broadcasts fresh 'overview'/'api_directory' snapshots
  // every 10s over the shared WebSocket — no more REST polling from here.
  await overviewStore.fetchOverview()
  await fetchApiDirectory()
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

useWebSocketTopic<MetricsData>('metrics', (data) => {
  liveMetrics.value = data
})

useWebSocketTopic<OverviewSnapshot>('overview', (data) => {
  overviewStore.setData(data as any)
})

useWebSocketTopic<{ entries: ApiDirectoryEntry[] }>('api_directory', (data) => {
  if (data && Array.isArray(data.entries)) apiDirectory.value = data.entries
})

useWebSocketTopic<LoadProgressData>('load_progress', (data) => {
  progressMessage.value = `${data.model_name}: ${data.stage} - ${data.message}`
})

async function unloadModel(name: string): Promise<void> {
  if (!confirm(`确认要卸载模型 ${name} 吗？这将停止其推理服务。`)) return
  unloadingModel.value = name
  try {
    await modelsStore.unloadModel(name)
    await overviewStore.fetchOverview()
  } catch (err: any) {
    progressMessage.value = err?.response?.data?.detail || `卸载 ${name} 失败`
  } finally {
    unloadingModel.value = null
  }
}

// 清理加载失败的模型：容器已崩溃退出，这里 down 掉它并从登记表移除。
async function cleanupModel(name: string): Promise<void> {
  if (!confirm(`模型 ${name} 加载失败（容器已退出）。确认清理该失败容器吗？`)) return
  unloadingModel.value = name
  try {
    await modelsStore.unloadModel(name)
    await overviewStore.fetchOverview()
  } catch (err: any) {
    progressMessage.value = err?.response?.data?.detail || `清理 ${name} 失败`
  } finally {
    unloadingModel.value = null
  }
}

async function fixCheck(name: string): Promise<void> {
  if (!confirm(`确认要执行 ${name} 的修复操作吗？该操作会在服务器上执行命令。`)) return
  try {
    await apiClient.post(`/api/env/fix/${name}`, { confirmed: true })
    await overviewStore.fetchOverview()
  } catch (err: any) {
    alert(err?.response?.data?.detail || '修复失败')
  }
}

async function componentAction(name: string, action: 'start' | 'stop' | 'restart'): Promise<void> {
  try {
    await apiClient.post(`/api/components/${encodeURIComponent(name)}/${action}`)
    await overviewStore.fetchOverview()
  } catch (err: any) {
    alert(err?.response?.data?.detail || `${action} 失败`)
  }
}

function fmt(n: number | null | undefined, unit = ''): string {
  if (n === null || n === undefined) return '--'
  return `${n}${unit}`
}

const envLabels: Record<string, string> = {
  cuda_compat: 'CUDA兼容包',
  ethernet_speed: '网卡状态',
  drop_caches: 'drop_caches',
  swap: '交换分区',
}

const allEnvOk = (checks: { status: string }[] | undefined) =>
  !!checks && checks.every((c) => c.status === 'ok')
</script>

<template>
  <div class="overview">
    <p v-if="overviewStore.error" class="error-banner">{{ overviewStore.error }}</p>
    <p v-if="progressMessage" class="progress-banner">{{ progressMessage }}</p>

    <!-- Highest priority: model load status + unload action -->
    <section class="card model-card">
      <h2>模型加载状态</h2>
      <div class="model-row">
        <span class="model-row-label">通用大模型</span>
        <div class="model-chip-list">
          <template v-if="overviewStore.data?.model_load.general_models_loaded.length || overviewStore.data?.model_load.general_models_failed.length">
            <span
              v-for="name in overviewStore.data.model_load.general_models_loaded"
              :key="name"
              class="model-chip"
            >
              <span class="status-dot status-ok" />
              {{ name }}
              <button
                class="btn danger chip-unload"
                :disabled="unloadingModel === name"
                @click="unloadModel(name)"
              >
                {{ unloadingModel === name ? '卸载中...' : '卸载' }}
              </button>
            </span>
            <span
              v-for="name in overviewStore.data.model_load.general_models_failed"
              :key="'failed-' + name"
              class="model-chip failed"
            >
              <span class="status-dot status-error" />
              {{ name }} · 加载失败
              <button
                class="btn secondary chip-unload"
                :disabled="unloadingModel === name"
                @click="cleanupModel(name)"
              >
                {{ unloadingModel === name ? '清理中...' : '清理' }}
              </button>
            </span>
          </template>
          <span v-else class="muted">未加载</span>
        </div>
      </div>
      <div class="model-row">
        <span class="model-row-label">嵌入式模型</span>
        <div class="model-chip-list">
          <template v-if="overviewStore.data?.model_load.embedding_models_loaded.length || overviewStore.data?.model_load.embedding_models_failed.length">
            <span
              v-for="name in overviewStore.data.model_load.embedding_models_loaded"
              :key="name"
              class="model-chip"
            >
              <span class="status-dot status-ok" />
              {{ name }}
              <button
                class="btn danger chip-unload"
                :disabled="unloadingModel === name"
                @click="unloadModel(name)"
              >
                {{ unloadingModel === name ? '卸载中...' : '卸载' }}
              </button>
            </span>
            <span
              v-for="name in overviewStore.data.model_load.embedding_models_failed"
              :key="'failed-' + name"
              class="model-chip failed"
            >
              <span class="status-dot status-error" />
              {{ name }} · 加载失败
              <button
                class="btn secondary chip-unload"
                :disabled="unloadingModel === name"
                @click="cleanupModel(name)"
              >
                {{ unloadingModel === name ? '清理中...' : '清理' }}
              </button>
            </span>
          </template>
          <span v-else class="muted">未加载</span>
        </div>
      </div>
    </section>

    <!-- System resources: compact gauge-style mini cards, high glance frequency -->
    <section class="card">
      <h2>系统资源</h2>
      <div class="res-grid">
        <div class="res-block">
          <div class="res-label">CPU 使用率</div>
          <div class="res-value">{{ fmt(liveMetrics?.cpu_percent ?? overviewStore.data?.resources.cpu_percent, '%') }}</div>
        </div>
        <div class="res-block">
          <div class="res-label">GPU 使用率</div>
          <div class="res-value">{{ fmt(liveMetrics?.gpu_percent ?? overviewStore.data?.resources.gpu_percent, '%') }}</div>
        </div>
        <div class="res-block">
          <div class="res-label">系统功耗</div>
          <div class="res-value">{{ fmt(liveMetrics?.power_watts ?? overviewStore.data?.resources.power_watts, 'W') }}</div>
        </div>
        <div class="res-block">
          <div class="res-label">使用内存</div>
          <div class="res-value">{{ fmt(liveMetrics?.mem_used_gb ?? overviewStore.data?.resources.mem_used_gb, 'GB') }}</div>
        </div>
        <div class="res-block">
          <div class="res-label">剩余内存</div>
          <div class="res-value">{{ fmt(liveMetrics?.mem_free_gb ?? overviewStore.data?.resources.mem_free_gb, 'GB') }}</div>
        </div>
        <div class="res-block">
          <div class="res-label">系统缓存大小</div>
          <div class="res-value">{{ fmt(liveMetrics?.cache_gb ?? overviewStore.data?.resources.cache_gb, 'GB') }}</div>
        </div>
      </div>
    </section>

    <!-- Env checks: compact status strip when all-ok, expands detail per item otherwise -->
    <section class="card">
      <h2>环境优化状态</h2>
      <div v-if="allEnvOk(overviewStore.data?.env.checks)" class="env-strip ok">
        <span class="status-dot status-ok" />
        全部环境检查正常
        <span class="env-strip-items">
          <span v-for="check in overviewStore.data?.env.checks ?? []" :key="check.name" class="env-strip-item">
            {{ envLabels[check.name] ?? check.name }}
          </span>
        </span>
      </div>
      <div v-else class="env-list">
        <div
          v-for="check in overviewStore.data?.env.checks ?? []"
          :key="check.name"
          class="env-block"
          :class="`status-${check.status}`"
        >
          <span class="status-dot" :class="`status-${check.status}`" />
          <div class="env-text">
            <div class="env-name">{{ envLabels[check.name] ?? check.name }}</div>
            <div class="env-message">{{ check.message }}</div>
            <div v-if="check.fixable && check.status !== 'ok'" class="env-cli-hint">
              或在服务器执行: python -m cli.main start 进行修复{{ check.suggested_command ? `（对应命令: ${check.suggested_command}）` : '' }}
            </div>
          </div>
          <button v-if="check.fixable && check.status !== 'ok'" class="btn fix-btn" @click="fixCheck(check.name)">
            立即修复
          </button>
        </div>
      </div>
    </section>

    <!-- Component status: table for scanning multiple rows -->
    <section class="card">
      <h2>组件状态</h2>
      <table>
        <thead>
          <tr>
            <th>组件名称</th>
            <th>容器名称</th>
            <th>网络端口（绑定IP）</th>
            <th>内存使用</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in overviewStore.data?.components ?? []" :key="c.name">
            <td>{{ c.name }}</td>
            <td>{{ c.container_name }}</td>
            <td>
              <template v-if="c.port != null">
                {{ c.bind_host ? `${c.bind_host}:${c.port}` : c.port }}
                <span class="bind-hint" :class="c.bind_host === '0.0.0.0' ? 'lan' : 'local'">
                  {{ c.bind_host === '0.0.0.0' ? '局域网可访问' : (c.bind_host === '127.0.0.1' ? '仅本机' : '') }}
                </span>
              </template>
              <template v-else>--</template>
            </td>
            <td>{{ c.memory_usage_mb != null ? `${c.memory_usage_mb} MB` : '--' }}</td>
            <td>
              <span
                class="status-dot"
                :class="c.status === 'running' ? 'status-ok' : ['error','failed','exited','dead'].includes(c.status) ? 'status-error' : 'status-warning'"
              />
              {{ c.status }}
              <div v-if="c.detail" class="muted" style="font-size:12px; margin-top:2px">{{ c.detail }}</div>
            </td>
            <td class="comp-actions">
              <template v-if="c.manageable">
                <button class="btn secondary" @click="componentAction(c.name, 'start')">启动</button>
                <button class="btn danger" @click="componentAction(c.name, 'stop')">停止</button>
                <button class="btn secondary" @click="componentAction(c.name, 'restart')">重启</button>
              </template>
              <span v-else class="muted">--</span>
            </td>
          </tr>
          <tr v-if="!overviewStore.data?.components.length">
            <td colspan="6" class="muted">暂无已加载组件</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- API 发布: Web/API endpoint directory + SearXNG URL format -->
    <section class="card">
      <div class="api-header">
        <h2>API 发布</h2>
        <span
          v-if="apiHealthOverall !== null"
          class="api-overall"
          :class="apiHealthOverall ? 'ok' : 'warn'"
        >
          <span class="status-dot" :class="apiHealthOverall ? 'status-ok' : 'status-warning'" />
          {{ apiHealthOverall ? '全部可达' : '存在不可达接口' }}
          <span class="api-checked-at" v-if="apiHealthCheckedAt">（{{ apiHealthCheckedAt }}）</span>
        </span>
        <button class="btn" :disabled="apiHealthChecking" @click="runApiHealthCheck">
          {{ apiHealthChecking ? '检测中...' : '检测API健康状态' }}
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>用途</th>
            <th>API BaseURL</th>
            <th>密钥</th>
            <th>健康状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in apiDirectory" :key="entry.name">
            <td>{{ entry.name }}</td>
            <td>{{ entry.purpose }}</td>
            <td class="api-url">{{ entry.base_url ?? '--' }}</td>
            <td>
              <span v-if="entry.name === 'Web管理后台API'">
                {{ entry.auth_hint }}（密码见「高级设置」页）
              </span>
              <span v-else>{{ entry.auth_hint }}</span>
            </td>
            <td>
              <template v-if="apiHealth[entry.name]">
                <span
                  class="status-dot"
                  :class="apiHealth[entry.name].healthy ? 'status-ok' : 'status-error'"
                />
                {{ apiHealth[entry.name].healthy ? '正常' : '异常' }}
                <span class="api-health-detail">
                  {{ apiHealth[entry.name].detail }}
                  <template v-if="apiHealth[entry.name].latency_ms != null">
                    ({{ apiHealth[entry.name].latency_ms }}ms)
                  </template>
                </span>
              </template>
              <span v-else class="muted">未检测</span>
            </td>
          </tr>
          <tr v-if="!apiDirectory.length">
            <td colspan="5" class="muted">加载中...</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.overview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.error-banner, .progress-banner {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-base);
}
.error-banner { color: var(--error); }
.progress-banner { color: var(--info); }

/* Top priority model card gets stronger visual weight */
.model-card {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px rgba(37, 99, 235, 0.15);
}
.model-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.model-row:last-child {
  margin-bottom: 0;
}
.model-row-label {
  font-size: var(--fs-base);
  color: var(--text-muted);
  width: 90px;
  padding-top: 6px;
  flex-shrink: 0;
}
.model-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  flex: 1;
}
.model-chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  font-size: var(--fs-md);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.model-chip.failed {
  border-color: var(--error, #ef4444);
  color: var(--error, #ef4444);
}
.chip-unload {
  font-size: var(--fs-xs);
  padding: 3px 8px;
}
.muted {
  color: var(--text-faint);
  font-size: var(--fs-base);
}

/* Resources: responsive auto-fit grid instead of fixed 6 columns */
.res-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--sp-3);
}
.res-block {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
}
.res-label {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.res-value {
  font-size: var(--fs-xl);
  font-weight: 600;
  margin-top: var(--sp-1);
}

/* Env checks: compact strip when healthy, detail list otherwise */
.env-strip {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius-sm);
  background: var(--ok-bg);
  font-size: var(--fs-base);
  color: var(--ok);
  flex-wrap: wrap;
}
.env-strip-items {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-left: auto;
}
.env-strip-item {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  background: var(--surface-2);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
}
.env-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--sp-3);
}
.env-block {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
}
.env-block.status-warning {
  border-color: var(--warning);
  background: var(--warning-bg);
}
.env-block.status-error {
  border-color: var(--error);
  background: var(--error-bg);
}
.env-text {
  flex: 1;
}
.env-name {
  font-size: var(--fs-base);
  font-weight: 600;
}
.env-message {
  font-size: var(--fs-xs);
  color: var(--text-faint);
  margin-top: 2px;
}
.env-cli-hint {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-top: 4px;
  font-style: italic;
}
.fix-btn {
  font-size: var(--fs-xs);
  padding: 4px 8px;
  flex-shrink: 0;
}
.comp-actions {
  display: flex;
  gap: var(--sp-1);
}
.comp-actions .btn {
  padding: 4px 8px;
  font-size: var(--fs-xs);
}
.api-url {
  font-family: monospace;
  font-size: var(--fs-xs);
  word-break: break-all;
}
.bind-hint {
  font-size: var(--fs-xs);
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}
.bind-hint.lan {
  color: var(--ok);
  background: var(--ok-bg);
}
.bind-hint.local {
  color: var(--warning);
  background: var(--warning-bg);
}
.api-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
  flex-wrap: wrap;
}
.api-header h2 {
  margin: 0;
}
.api-header .btn {
  margin-left: auto;
}
.api-overall {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
}
.api-overall.ok {
  color: var(--ok);
}
.api-overall.warn {
  color: var(--warning);
}
.api-checked-at {
  color: var(--text-faint);
  font-size: var(--fs-xs);
}
.api-health-detail {
  display: block;
  font-size: var(--fs-xs);
  color: var(--text-faint);
  margin-top: 2px;
}
</style>
