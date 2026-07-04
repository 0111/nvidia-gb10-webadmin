<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import apiClient from '@/api/client'
import { useWebSocketTopic } from '@/composables/useWebSocket'
import type { ModelInfoOut, PerfProgress, PerfReportListItem, PerfReportOut } from '@/types/api'

// 性能测试页：轻量级自定义并发吞吐测试，对接 /api/perf/run + /api/perf/reports。
// 注意：本页不接入 MLPerf/LLMPerf 等国际标准基准——那需要独立的题库/打分
// 体系，超出本阶段范围（详见 研发方案.md 阶段五），仅做自定义并发压测。

const generalModels = ref<ModelInfoOut[]>([])
const selectedModelName = ref('')
const concurrency = ref(4)
const numRequests = ref(8)
const prompt = ref('请用一段话介绍一下你自己。')
const maxTokens = ref(256)
const temperature = ref(0.7)

const running = ref(false)
const errorMsg = ref('')
const lastReport = ref<PerfReportOut | null>(null)
const reports = ref<PerfReportListItem[]>([])

// 实时进度（来自后端 perf_progress WS 主题）
const progress = ref<PerfProgress | null>(null)
const progressPct = computed(() => {
  const p = progress.value
  if (!p || !p.total) return 0
  return Math.round((p.completed / p.total) * 100)
})
// 展开查看出错详情的行
const expandedErrors = ref<Set<number>>(new Set())
function toggleError(idx: number): void {
  const s = new Set(expandedErrors.value)
  s.has(idx) ? s.delete(idx) : s.add(idx)
  expandedErrors.value = s
}
function pretty(obj: any): string {
  try {
    return typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

useWebSocketTopic<PerfProgress>('perf_progress', (data) => {
  // 只在测试运行期间更新进度（忽略陈旧/其它来源的消息）
  if (running.value) progress.value = data
})

async function loadModels(): Promise<void> {
  const { data } = await apiClient.get('/api/models', { params: { type: 'general' } })
  generalModels.value = data.models.filter((m: ModelInfoOut) => m.load_status === 'running')
  if (generalModels.value.length && !selectedModelName.value) {
    selectedModelName.value = generalModels.value[0].name
  }
}

async function loadReports(): Promise<void> {
  const { data } = await apiClient.get('/api/perf/reports')
  reports.value = data.reports
}

onMounted(async () => {
  await Promise.all([loadModels(), loadReports()])
})

async function runTest(): Promise<void> {
  if (!selectedModelName.value) {
    errorMsg.value = '请先选择一个已加载的通用模型'
    return
  }
  errorMsg.value = ''
  running.value = true
  expandedErrors.value = new Set()
  progress.value = { report_id: '', model_name: selectedModelName.value, completed: 0, total: numRequests.value, failed: 0, stage: 'start' }
  try {
    const { data } = await apiClient.post<PerfReportOut>('/api/perf/run', {
      model_name: selectedModelName.value,
      concurrency: concurrency.value,
      num_requests: numRequests.value,
      prompt: prompt.value,
      max_tokens: maxTokens.value,
      temperature: temperature.value,
      stream: false,
    })
    lastReport.value = data
    await loadReports()
  } catch (err: any) {
    errorMsg.value = err?.response?.data?.detail || '测试执行失败'
  } finally {
    running.value = false
  }
}

async function openReport(reportId: string): Promise<void> {
  const { data } = await apiClient.get<PerfReportOut>(`/api/perf/reports/${reportId}`)
  lastReport.value = data
}

async function deleteReport(reportId: string): Promise<void> {
  if (!confirm('确认删除该条测试记录吗？此操作不可恢复。')) return
  try {
    await apiClient.delete(`/api/perf/reports/${reportId}`)
    if (lastReport.value?.report_id === reportId) lastReport.value = null
    await loadReports()
  } catch (err: any) {
    errorMsg.value = err?.response?.data?.detail || '删除失败'
  }
}

async function clearAllReports(): Promise<void> {
  if (!reports.value.length) return
  if (!confirm(`确认清空全部 ${reports.value.length} 条历史测试记录吗？此操作不可恢复。`)) return
  try {
    await apiClient.delete('/api/perf/reports')
    lastReport.value = null
    await loadReports()
  } catch (err: any) {
    errorMsg.value = err?.response?.data?.detail || '清空失败'
  }
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}
</script>

<template>
  <div class="performance-test">
    <section class="card">
      <h2>测试参数配置（轻量级自定义并发吞吐测试，未接入 MLPerf/LLMPerf 等标准基准）</h2>
      <div class="form-grid">
        <label>
          目标模型（需已加载）
          <select v-model="selectedModelName">
            <option v-for="m in generalModels" :key="m.name" :value="m.name">{{ m.name }}</option>
          </select>
        </label>
        <label>
          并发数
          <input type="number" v-model.number="concurrency" min="1" max="64" />
        </label>
        <label>
          请求总数
          <input type="number" v-model.number="numRequests" min="1" max="500" />
        </label>
        <label class="full-width">
          测试 Prompt
          <textarea v-model="prompt" rows="2"></textarea>
        </label>
      </div>

      <details class="collapsible advanced-params">
        <summary>高级参数</summary>
        <div class="collapsible-body form-grid">
          <label>
            输出长度（max_tokens）
            <input type="number" v-model.number="maxTokens" min="1" />
          </label>
          <label>
            Temperature
            <input type="number" v-model.number="temperature" step="0.1" min="0" max="2" />
          </label>
        </div>
      </details>

      <div class="actions-row">
        <button class="btn" :disabled="running || !generalModels.length" @click="runTest">
          {{ running ? '测试运行中...' : '开始测试' }}
        </button>
        <span v-if="!generalModels.length" class="muted">当前没有已加载的通用模型，请先在“通用大模型配置”页加载一个</span>
        <span v-if="errorMsg" class="error-text">{{ errorMsg }}</span>
      </div>

      <div v-if="running || progress" class="progress-box">
        <div class="progress-bar-track">
          <div class="progress-bar-fill" :style="{ width: progressPct + '%' }" />
        </div>
        <div class="progress-text">
          进度 {{ progress?.completed ?? 0 }} / {{ progress?.total ?? numRequests }}（{{ progressPct }}%）
          <span v-if="progress?.failed" class="error-text">· 失败 {{ progress.failed }}</span>
          <span v-if="!running && progress?.stage === 'done'" class="muted">· 已完成</span>
        </div>
      </div>
    </section>

    <section v-if="lastReport" class="card">
      <h2>本次测试结果 — {{ lastReport.model_name }}</h2>
      <div class="summary-grid">
        <div class="summary-cell"><div class="label">总请求数</div><div class="value">{{ lastReport.summary.total_requests }}</div></div>
        <div class="summary-cell"><div class="label">成功/失败</div><div class="value">{{ lastReport.summary.successful }} / {{ lastReport.summary.failed }}</div></div>
        <div class="summary-cell"><div class="label">总耗时</div><div class="value">{{ lastReport.summary.total_duration_ms }} ms</div></div>
        <div class="summary-cell"><div class="label">平均单请求耗时</div><div class="value">{{ lastReport.summary.avg_total_ms ?? '--' }} ms</div></div>
        <div class="summary-cell"><div class="label">平均 TTFT</div><div class="value">{{ lastReport.summary.avg_ttft_ms ?? '--' }} ms</div></div>
        <div class="summary-cell"><div class="label">吞吐量</div><div class="value">{{ lastReport.summary.throughput_tokens_per_sec ?? '--' }} tok/s</div></div>
      </div>
      <table>
        <thead>
          <tr><th>#</th><th>状态</th><th>耗时(ms)</th><th>completion tokens</th><th>错误 / 详情</th></tr>
        </thead>
        <tbody>
          <template v-for="r in lastReport.results" :key="r.index">
            <tr :class="{ 'row-failed': !r.ok }">
              <td>{{ r.index }}</td>
              <td>
                <span class="status-dot" :class="r.ok ? 'status-ok' : 'status-error'" />
                {{ r.ok ? '成功' : '失败' }}
              </td>
              <td>{{ r.total_ms ?? '--' }}</td>
              <td>{{ r.completion_tokens ?? '--' }}</td>
              <td>
                <span v-if="r.ok" class="muted">--</span>
                <template v-else>
                  <span class="error-text">{{ r.error }}</span>
                  <button class="link-btn" @click="toggleError(r.index)">
                    {{ expandedErrors.has(r.index) ? '收起' : '查看请求/响应' }}
                  </button>
                </template>
              </td>
            </tr>
            <tr v-if="!r.ok && expandedErrors.has(r.index)" class="error-detail-row">
              <td colspan="5">
                <div class="error-detail">
                  <div class="error-detail-title">请求包（request）</div>
                  <pre class="excerpt-box">{{ r.request_excerpt ? pretty(r.request_excerpt) : '（无）' }}</pre>
                  <div class="error-detail-title">响应包（response，状态 {{ r.status_code ?? '无' }}）</div>
                  <pre class="excerpt-box">{{ r.response_excerpt || '（无响应体，可能是连接超时/被拒绝等网络层错误）' }}</pre>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </section>

    <section class="card">
      <div class="history-header">
        <h2>历史测试记录</h2>
        <button class="btn danger" :disabled="!reports.length" @click="clearAllReports">清空全部</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>模型</th>
            <th>并发</th>
            <th>请求数</th>
            <th>成功/失败</th>
            <th>吞吐 tok/s</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!reports.length">
            <td colspan="7" class="muted">暂无测试记录</td>
          </tr>
          <tr v-for="r in reports" :key="r.report_id">
            <td>{{ fmtTime(r.created_at) }}</td>
            <td>{{ r.model_name }}</td>
            <td>{{ r.concurrency }}</td>
            <td>{{ r.num_requests }}</td>
            <td>{{ r.summary.successful }} / {{ r.summary.failed }}</td>
            <td>{{ r.summary.throughput_tokens_per_sec ?? '--' }}</td>
            <td class="row-actions">
              <button class="link-btn" @click="openReport(r.report_id)">查看详情</button>
              <button class="link-btn danger-link" @click="deleteReport(r.report_id)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.full-width {
  grid-column: 1 / -1;
}
.advanced-params {
  margin-top: var(--sp-3);
}
.actions-row {
  margin-top: var(--sp-3);
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.error-text {
  color: var(--error);
  font-size: var(--fs-sm);
}
.performance-test {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.summary-cell {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
}
.summary-cell .label {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.summary-cell .value {
  font-size: var(--fs-lg);
  font-weight: 600;
  margin-top: 2px;
}
.link-btn {
  background: none;
  border: none;
  color: var(--info);
  cursor: pointer;
  font-size: var(--fs-sm);
  padding: 0;
}
.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.history-header h2 {
  margin: 0;
}
.row-actions {
  display: flex;
  gap: var(--sp-3);
}
.danger-link {
  color: var(--error);
}
.progress-box {
  margin-top: var(--sp-3);
}
.progress-bar-track {
  height: 8px;
  background: var(--surface-3);
  border-radius: 999px;
  overflow: hidden;
}
.progress-bar-fill {
  height: 100%;
  background: var(--accent, #2563eb);
  transition: width 0.3s ease;
}
.progress-text {
  margin-top: 6px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.row-failed td {
  background: var(--error-bg);
}
.error-detail-row td {
  background: var(--surface-2);
}
.error-detail-title {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  font-weight: 600;
  margin: var(--sp-2) 0 4px;
}
.excerpt-box {
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-2);
  font-size: var(--fs-xs);
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
