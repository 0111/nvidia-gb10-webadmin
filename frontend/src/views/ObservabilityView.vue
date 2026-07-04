<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import apiClient from '@/api/client'
import Sparkline from '@/components/Sparkline.vue'
import { useWebSocketTopic } from '@/composables/useWebSocket'
import type { MetricsData, MetricsHistoryPoint } from '@/types/api'

// 运行观测页：
// - 系统类 4 个图表（内存/GPU负载/GPU温度/功耗）接入真实历史数据
//   GET /api/metrics/history?window=1d，由后端 web/background_tasks.py
//   每 10 秒采集时追加写入 data/metrics_history.jsonl。
// - 模型 tok/s 4 个图表（通用/嵌入 prefill/decode）：当前后端的
//   MetricsCollector 不采集 vllm 运行时的 prefill/decode tok/s
//   （vllm 暴露这类指标的接口尚未被本系统抓取），故继续展示为
//   "数据源待补充" 占位，不假造数据——后续阶段如需实现，应在
//   background_tasks.py 增加对 /metrics (vllm Prometheus 格式) 的
//   解析并写入同一份历史存储。

const liveMetrics = ref<MetricsData | null>(null)
const history = ref<MetricsHistoryPoint[]>([])
const loadingHistory = ref(false)

useWebSocketTopic<MetricsData>('metrics', (data) => {
  liveMetrics.value = data
})

interface RuntimeStats {
  model_name: string
  loaded: boolean
  message?: string
  engine?: string
  container_name?: string
  host_port?: number | null
  launch_command?: string[] | null
  memory_usage_mb?: number | null
  metrics?: {
    num_requests_running: number | null
    num_requests_waiting: number | null
    gpu_cache_usage_perc: number | null
    prefill_tps?: number | null
    decode_tps?: number | null
    prompt_tokens_total?: number | null
    generation_tokens_total?: number | null
  } | null
  metrics_message?: string | null
}

const generalModels = ref<any[]>([])
const embeddingModels = ref<any[]>([])
const runtimeStatsByModel = ref<Record<string, RuntimeStats>>({})

// runtime-stats (内存/KV Cache/请求数/tok-s/启动参数) 现由后端每 10s 通过
// 'runtime_stats' WS 主题推送 {模型名: stats}，本页不再各自轮询
// /api/models/{name}/runtime-stats。tok/s 历史曲线则与系统级指标一样取自
// 持久化的 /api/metrics/history（model_tps 字段），见下方 tpsSeries。
useWebSocketTopic<Record<string, RuntimeStats>>('runtime_stats', (data) => {
  if (data && typeof data === 'object') {
    runtimeStatsByModel.value = { ...runtimeStatsByModel.value, ...data }
  }
})

const loadedModelStats = computed(() =>
  [...generalModels.value, ...embeddingModels.value]
    .filter((m: any) => runtimeStatsByModel.value[m.name]?.loaded)
    .map((m: any) => ({
      name: m.name,
      is_embedding: !!m.is_embedding,
      stats: runtimeStatsByModel.value[m.name],
    })),
)

async function loadHistory(): Promise<void> {
  loadingHistory.value = true
  try {
    const { data } = await apiClient.get('/api/metrics/history', { params: { window: '1d' } })
    history.value = data.points
  } finally {
    loadingHistory.value = false
  }
}

async function loadRuntimeStats(modelName: string): Promise<void> {
  try {
    const { data } = await apiClient.get<RuntimeStats>(`/api/models/${encodeURIComponent(modelName)}/runtime-stats`)
    runtimeStatsByModel.value = { ...runtimeStatsByModel.value, [modelName]: data }
  } catch {
    // leave previous value (if any); a transient failure shouldn't blank the row
  }
}

async function refreshAllRuntimeStats(): Promise<void> {
  const names = [...generalModels.value, ...embeddingModels.value].map((m) => m.name)
  await Promise.all(names.map(loadRuntimeStats))
}

let runtimeStatsTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  const [generalResp, embeddingResp] = await Promise.all([
    apiClient.get('/api/models', { params: { type: 'general' } }),
    apiClient.get('/api/models', { params: { type: 'embedding' } }),
    loadHistory(),
  ])
  generalModels.value = generalResp.data.models.filter((m: any) => m.load_status === 'running')
  embeddingModels.value = embeddingResp.data.models.filter((m: any) => m.load_status === 'running')
  // Seed once via REST for an instant first paint; subsequent updates arrive
  // over the 'runtime_stats' WebSocket topic (pushed every 10s by the
  // backend MetricsCollector), so no client-side poll timer is needed.
  await refreshAllRuntimeStats()
})

onUnmounted(() => {
  if (runtimeStatsTimer) clearInterval(runtimeStatsTimer)
})

// 运行观测的"量化"列：已量化模型直接显示量化方法；未量化模型
// (quantization=None) 不再显示空洞的"--"，而是显示其权重精度 dtype
// 并标注"未量化"，避免被误读为"数据缺失/显示错误"。
function quantLabel(m: any): string {
  if (m?.quantization) return m.quantization
  if (m?.torch_dtype) return `未量化（${m.torch_dtype}）`
  return '--'
}

function seriesOf(key: keyof MetricsHistoryPoint): number[] {
  return history.value.map((p) => (p[key] as number | null) ?? NaN)
}

// 该模型在 1 天窗口里首次出现 tok/s 数据的下标（prefill 或 decode 任一）。
// 模型可能是最近才加载的——此前的历史点对它没有数据，若把整天窗口都画出来，
// 数据会被挤到最右侧一小条、左侧一大片空白（很不直观）。因此按模型裁剪到
// 「自其开始产生数据」起，让实际活跃区间铺满整宽。prefill/decode 共用同一起点，
// 保证同一模型两图横轴对齐。
function modelDataStart(modelName: string): number {
  const h = history.value
  for (let i = 0; i < h.length; i++) {
    const mt = h[i].model_tps?.[modelName]
    if (mt && (mt.prefill_tps != null || mt.decode_tps != null)) return i
  }
  return 0
}

// 模型级 tok/s 历史序列：从持久化的 /api/metrics/history 派生（与系统级同源），
// 裁剪到该模型的活跃区间起点。
function tpsSeries(modelName: string, kind: 'prefill_tps' | 'decode_tps'): number[] {
  const start = modelDataStart(modelName)
  const out: number[] = []
  for (let i = start; i < history.value.length; i++) {
    const v = history.value[i].model_tps?.[modelName]?.[kind]
    out.push(v == null ? NaN : v)
  }
  return out
}

const memSeries = computed(() => seriesOf('mem_used_gb'))
const gpuSeries = computed(() => seriesOf('gpu_percent'))
const tempSeries = computed(() => seriesOf('gpu_temp_c'))
const powerSeries = computed(() => seriesOf('power_watts'))

function metricValue(key: string): string {
  if (!liveMetrics.value) return '--'
  const v = (liveMetrics.value as any)[key]
  return v === null || v === undefined ? '--' : String(v)
}
</script>

<template>
  <div class="observability">
    <!-- System-level: higher glance frequency, given more visual weight -->
    <section class="card">
      <h2>系统级指标（近 1 天，10 秒采集一次）</h2>
      <div class="chart-grid system-grid">
        <div class="chart-slot">
          <div class="chart-title">内存使用率（已用 GB）</div>
          <div class="chart-value">{{ metricValue('mem_used_gb') }}</div>
          <Sparkline :points="memSeries" unit="GB" color="#22c55e" />
        </div>
        <div class="chart-slot">
          <div class="chart-title">GPU 负载率（%）</div>
          <div class="chart-value">{{ metricValue('gpu_percent') }}</div>
          <Sparkline :points="gpuSeries" unit="%" color="#6ea8fe" />
        </div>
        <div class="chart-slot">
          <div class="chart-title">GPU 温度（℃）</div>
          <div class="chart-value">{{ metricValue('gpu_temp_c') }}</div>
          <Sparkline :points="tempSeries" unit="℃" color="#f59e0b" />
        </div>
        <div class="chart-slot">
          <div class="chart-title">设备功耗（W）</div>
          <div class="chart-value">{{ metricValue('power_watts') }}</div>
          <Sparkline :points="powerSeries" unit="W" color="#ef4444" />
        </div>
      </div>
    </section>

    <!-- Model-level: real prefill/decode tok/s per loaded model, derived from
         vLLM /metrics token counters (差分计算)，每 10s 随 runtime_stats 推送更新。 -->
    <section class="card model-level-card">
      <h2>模型级指标（tok/s，10 秒采集一次）</h2>
      <p class="muted section-note">
        取自 vLLM /metrics 的 prompt/generation token 计数器差分（prefill=输入侧，decode=输出侧）；与系统级指标同源持久化。
        空闲（无推理请求）时 tok/s 为 0 属正常；<b>prefill 天然呈脉冲状</b>（一次请求的输入在单个采样窗口内被处理完 → 该点很高、随后归零）。
        横轴按每个模型「自加载后开始产生数据」起绘制（新加载的模型时间跨度更短）。
      </p>
      <div v-if="loadedModelStats.length" class="model-tps-grid">
        <div v-for="item in loadedModelStats" :key="item.name" class="model-tps-card">
          <div class="model-tps-name"><span class="status-dot status-ok" /> {{ item.name }}</div>
          <div class="model-tps-charts">
            <div class="chart-slot">
              <div class="chart-title">Prefill tok/s（输入侧）</div>
              <div class="chart-value">{{ item.stats.metrics?.prefill_tps ?? '--' }}</div>
              <Sparkline :points="tpsSeries(item.name, 'prefill_tps')" :height="96" unit="" color="#22c55e" :show-peak="true" />
            </div>
            <div v-if="!item.is_embedding" class="chart-slot">
              <div class="chart-title">Decode tok/s（输出侧）</div>
              <div class="chart-value">{{ item.stats.metrics?.decode_tps ?? '--' }}</div>
              <Sparkline :points="tpsSeries(item.name, 'decode_tps')" :height="96" unit="" color="#6ea8fe" :show-peak="true" />
            </div>
            <div v-else class="chart-slot chart-slot-note">
              <div class="chart-title">Decode tok/s（输出侧）</div>
              <p class="muted">嵌入模型只做一次前向编码、无自回归生成，故无 decode 速率。</p>
            </div>
          </div>
        </div>
      </div>
      <p v-else class="muted">当前没有已加载的模型，加载后此处显示 prefill/decode tok/s 趋势。</p>
    </section>

    <section class="card">
      <h2>通用大模型 — 运行观测</h2>
      <div v-if="generalModels.length" class="runtime-grid">
        <div v-for="m in generalModels" :key="m.name" class="runtime-block">
          <div class="runtime-name">
            <span class="status-dot status-ok" />
            {{ m.name }}
          </div>
          <table>
            <tbody>
              <tr><td>模型文件大小</td><td>{{ m.size_gb }} GB</td></tr>
              <tr><td>量化</td><td>{{ quantLabel(m) }}</td></tr>
              <tr><td>上下文上限</td><td>{{ m.max_position_embeddings ?? '--' }}</td></tr>
              <tr><td>模型占用内存</td><td>{{ runtimeStatsByModel[m.name]?.memory_usage_mb != null ? `${runtimeStatsByModel[m.name].memory_usage_mb} MB` : '--' }}</td></tr>
              <tr><td>运行中请求数</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.num_requests_running ?? '--' }}</td></tr>
              <tr><td>等待中请求数</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.num_requests_waiting ?? '--' }}</td></tr>
              <tr><td>KV Cache 使用率</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.gpu_cache_usage_perc != null ? `${(runtimeStatsByModel[m.name].metrics!.gpu_cache_usage_perc! * 100).toFixed(1)}%` : '--' }}</td></tr>
              <tr><td>Prefill tok/s</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.prefill_tps ?? '--' }}</td></tr>
              <tr><td>Decode tok/s</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.decode_tps ?? '--' }}</td></tr>
              <tr><td>累计输入/输出 tokens</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.prompt_tokens_total ?? '--' }} / {{ runtimeStatsByModel[m.name]?.metrics?.generation_tokens_total ?? '--' }}</td></tr>
              <tr v-if="runtimeStatsByModel[m.name]?.metrics_message"><td colspan="2" class="muted">{{ runtimeStatsByModel[m.name]?.metrics_message }}</td></tr>
            </tbody>
          </table>
          <details class="launch-params">
            <summary>启动参数</summary>
            <pre class="result-box">{{ runtimeStatsByModel[m.name]?.launch_command?.join(' ') ?? '--' }}</pre>
          </details>
        </div>
      </div>
      <p v-else class="muted">当前没有已加载的通用大模型</p>
    </section>

    <section class="card">
      <h2>嵌入式模型 — 运行观测</h2>
      <div v-if="embeddingModels.length" class="runtime-grid">
        <div v-for="m in embeddingModels" :key="m.name" class="runtime-block">
          <div class="runtime-name">
            <span class="status-dot status-ok" />
            {{ m.name }}
          </div>
          <table>
            <tbody>
              <tr><td>模型文件大小</td><td>{{ m.size_gb }} GB</td></tr>
              <tr><td>量化</td><td>{{ quantLabel(m) }}</td></tr>
              <tr><td>上下文上限</td><td>{{ m.max_position_embeddings ?? '--' }}</td></tr>
              <tr><td>模型占用内存</td><td>{{ runtimeStatsByModel[m.name]?.memory_usage_mb != null ? `${runtimeStatsByModel[m.name].memory_usage_mb} MB` : '--' }}</td></tr>
              <tr><td>运行中请求数</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.num_requests_running ?? '--' }}</td></tr>
              <tr><td>等待中请求数</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.num_requests_waiting ?? '--' }}</td></tr>
              <tr><td>KV Cache 使用率</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.gpu_cache_usage_perc != null ? `${(runtimeStatsByModel[m.name].metrics!.gpu_cache_usage_perc! * 100).toFixed(1)}%` : '--' }}</td></tr>
              <tr><td>Prefill tok/s</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.prefill_tps ?? '--' }}</td></tr>
              <tr><td>Decode tok/s</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.decode_tps ?? '--' }}</td></tr>
              <tr><td>累计输入/输出 tokens</td><td>{{ runtimeStatsByModel[m.name]?.metrics?.prompt_tokens_total ?? '--' }} / {{ runtimeStatsByModel[m.name]?.metrics?.generation_tokens_total ?? '--' }}</td></tr>
              <tr v-if="runtimeStatsByModel[m.name]?.metrics_message"><td colspan="2" class="muted">{{ runtimeStatsByModel[m.name]?.metrics_message }}</td></tr>
            </tbody>
          </table>
          <details class="launch-params">
            <summary>启动参数</summary>
            <pre class="result-box">{{ runtimeStatsByModel[m.name]?.launch_command?.join(' ') ?? '--' }}</pre>
          </details>
        </div>
      </div>
      <p v-else class="muted">当前没有已加载的嵌入式模型</p>
    </section>
  </div>
</template>

<style scoped>
.observability {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}
/* Model-level tok/s: one card per model (name once), prefill + decode paired
   side by side inside — clearer than 4 separate slots repeating the name. */
.model-tps-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: var(--sp-3);
}
.model-tps-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
}
.model-tps-name {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-weight: 600;
  font-size: var(--fs-sm);
  margin-bottom: var(--sp-2);
  word-break: break-all;
}
.model-tps-charts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-3);
}
.model-tps-charts .chart-slot {
  background: var(--surface-3, var(--surface-1));
  min-height: 150px;
}
.chart-slot-note {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.chart-slot-note .muted {
  font-size: var(--fs-xs);
  margin: auto 0;
}
@media (max-width: 560px) {
  .model-tps-charts { grid-template-columns: 1fr; }
}
.chart-slot {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
  min-height: 110px;
}
.chart-title {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.chart-value {
  font-size: var(--fs-lg);
  font-weight: 600;
  margin: var(--sp-1) 0;
}
.model-grid .chart-slot.placeholder {
  min-height: 70px;
  background: var(--surface-3);
}
.chart-placeholder {
  font-size: var(--fs-xs);
  color: var(--text-disabled);
  margin-top: var(--sp-2);
}
.section-note {
  margin: 0 0 var(--sp-3);
}
.runtime-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--sp-3);
}
.runtime-block {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
}
.runtime-name {
  font-weight: 600;
  margin-bottom: var(--sp-2);
  display: flex;
  align-items: center;
}
.launch-params {
  margin-top: var(--sp-2);
  font-size: var(--fs-sm);
}
.launch-params summary {
  cursor: pointer;
  color: var(--text-muted);
}
.result-box {
  margin-top: var(--sp-2);
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-2);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
