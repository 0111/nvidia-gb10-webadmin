<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import apiClient from '@/api/client'
import { useWsStore } from '@/stores/ws'
import { useWebSocketTopic } from '@/composables/useWebSocket'

interface ComponentRow {
  name: string
  container_name: string | null
  port: number | null
}

// 下拉项标签：在组件名后附上容器名/端口，让"通用大模型容器(gb10-vllm-general,
// 8001)"、"嵌入式模型容器(gb10-vllm-embedding,8002)"等一眼可辨，避免用户
// 不清楚哪条对应 8001/8002 端口的模型日志。
function optionLabel(c: ComponentRow): string {
  const parts: string[] = []
  if (c.container_name) parts.push(c.container_name)
  if (c.port != null) parts.push(`:${c.port}`)
  return parts.length ? `${c.name}（${parts.join(' ')}）` : c.name
}

const wsStore = useWsStore()
const components = ref<ComponentRow[]>([])
const selectedComponent = ref('')
const lineCount = ref(200)
const lineOptions = [50, 100, 200, 500, 1000]
const logs = ref('')
const error = ref('')
const logBoxRef = ref<HTMLElement | null>(null)

// /api/logs/{component} understands container names (gb10-...) and the
// "web"/"frontend"/"searxng" aliases, but the two pidfile-tracked rows
// (Web管理后台/前端) have no container at all — map their display name to
// the alias the backend expects instead of sending the display name verbatim.
function logIdentifierFor(c: ComponentRow): string {
  if (c.name === 'Web管理后台') return 'web'
  if (c.name === '前端') return 'frontend'
  return c.container_name ?? c.name
}

// 日志改为 WebSocket 实时推送（topic: logs）：选择组件后告知后端 set_log_target，
// 后端每 ~10s 把该组件最新日志推给本连接，不再 REST 轮询。
async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (logBoxRef.value) logBoxRef.value.scrollTop = logBoxRef.value.scrollHeight
}

function applyLogTarget(): void {
  if (!selectedComponent.value) return
  const row = components.value.find((c) => c.name === selectedComponent.value)
  if (!row) return
  logs.value = '加载中...'
  wsStore.setLogTarget(logIdentifierFor(row), lineCount.value)
}

useWebSocketTopic<{ component: string; content: string }>('logs', (data) => {
  const row = components.value.find((c) => c.name === selectedComponent.value)
  if (!row || data.component !== logIdentifierFor(row)) return
  error.value = ''
  logs.value = data.content || '暂无日志'
  scrollToBottom()
})

onMounted(async () => {
  const { data } = await apiClient.get('/api/components')
  components.value = data.components
  if (components.value.length) {
    selectedComponent.value = components.value[0].name
  }
  applyLogTarget()
})

onUnmounted(() => {
  wsStore.setLogTarget(null)
})

watch(selectedComponent, () => applyLogTarget())
watch(lineCount, () => applyLogTarget())
</script>

<template>
  <div class="component-logs">
    <section class="card">
      <h2>组件日志</h2>
      <div class="controls">
        <label>
          选择组件
          <select v-model="selectedComponent">
            <option v-for="c in components" :key="c.name" :value="c.name">{{ optionLabel(c) }}</option>
          </select>
        </label>
        <label>
          显示行数
          <select v-model.number="lineCount">
            <option v-for="opt in lineOptions" :key="opt" :value="opt">{{ opt }}</option>
          </select>
        </label>
        <span class="muted">实时推送（WebSocket，约 10 秒刷新）</span>
        <button class="btn" @click="applyLogTarget">立即刷新</button>
      </div>
      <p v-if="error" class="error-text">{{ error }}</p>
      <p v-if="!components.length" class="muted">暂无已加载组件，请先在模型配置页加载模型</p>
      <pre ref="logBoxRef" class="log-box">{{ logs || '暂无日志' }}</pre>
    </section>
  </div>
</template>

<style scoped>
.controls {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  margin-bottom: var(--sp-3);
  flex-wrap: wrap;
}
.controls label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.auto-refresh {
  flex-direction: row !important;
  align-items: center;
}
.error-text {
  color: var(--error);
  font-size: var(--fs-sm);
}
.log-box {
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  max-height: 560px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
