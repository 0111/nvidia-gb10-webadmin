<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModelsStore } from '@/stores/models'
import { useWsStore } from '@/stores/ws'
import { useWebSocketTopic } from '@/composables/useWebSocket'
import type { LoadProgressData, ModelInfoOut, ParamEntryOut } from '@/types/api'

// This view covers 通用大模型配置: model picker, vllm param form
// (rendered entirely from backend /params data — no hardcoded param list),
// load action, and a 10s-refreshing log panel.

const props = defineProps<{ embedding?: boolean }>()

const modelsStore = useModelsStore()
const wsStore = useWsStore()
const models = ref<ModelInfoOut[]>([])
const selectedModelName = ref<string>('')
const engine = ref<'vllm'>('vllm')
const paramEntries = ref<Record<string, ParamEntryOut>>({})
const gpuMemHint = ref<import('@/types/api').GpuMemoryHint | null>(null)
const formValues = ref<Record<string, any>>({})
const loadMessage = ref('')
const loading = ref(false)

const logs = ref('')

const modelType = computed(() => (props.embedding ? 'embedding' : 'general'))
const componentName = computed(() => (props.embedding ? 'gb10-vllm-embedding' : 'gb10-vllm-general'))

const selectedModel = computed(() => models.value.find((m) => m.name === selectedModelName.value) ?? null)

const scannedAtText = computed(() =>
  modelsStore.scannedAt ? new Date(modelsStore.scannedAt * 1000).toLocaleString() : '尚未扫描',
)

async function rescanModels(): Promise<void> {
  try {
    const result = await modelsStore.rescan()
    models.value = await modelsStore.fetchModels(modelType.value)
    if (models.value.length && !models.value.find((m) => m.name === selectedModelName.value)) {
      selectedModelName.value = models.value[0].name
    }
    loadMessage.value = `扫描完成：共 ${result.total} 个模型，其中 ${result.invalid} 个文件校验未通过`
  } catch (err: any) {
    loadMessage.value = err?.response?.data?.detail || '重新扫描失败'
  }
}

onMounted(async () => {
  models.value = await modelsStore.fetchModels(modelType.value)
  if (models.value.length) {
    selectedModelName.value = models.value[0].name
  }
  // 该页固定查看本类型容器(gb10-vllm-general/embedding)的日志，走 WS 实时推送。
  logs.value = '加载中...'
  wsStore.setLogTarget(componentName.value, 200)
})

onUnmounted(() => {
  wsStore.setLogTarget(null)
})

useWebSocketTopic<LoadProgressData>('load_progress', (data) => {
  if (data.model_name === selectedModelName.value) {
    loadMessage.value = `${data.stage}: ${data.message}`
  }
})

useWebSocketTopic<{ component: string; content: string }>('logs', (data) => {
  if (data.component === componentName.value) {
    logs.value = data.content || '暂无日志'
  }
})

watch(selectedModelName, async (name) => {
  if (!name) return
  const resp = await modelsStore.fetchParams(name)
  engine.value = resp.engine
  paramEntries.value = resp.params
  gpuMemHint.value = resp.gpu_memory_hint ?? null
  formValues.value = Object.fromEntries(Object.entries(resp.params).map(([k, v]) => [k, v.default]))
})

async function loadModel(): Promise<void> {
  if (!selectedModelName.value) return
  loading.value = true
  loadMessage.value = '正在提交加载请求...'
  try {
    const resp = await modelsStore.loadModel(selectedModelName.value, engine.value, formValues.value)
    loadMessage.value = resp.message
    models.value = await modelsStore.fetchModels(modelType.value)
    // 重新触发一次日志推送（刷新启动日志）
    wsStore.setLogTarget(componentName.value, 200)
  } catch (err: any) {
    loadMessage.value = err?.response?.data?.detail || '加载失败'
  } finally {
    loading.value = false
  }
}

// 多模态列显示：支持的输入模态（图片/视频/音频），纯文本模型显示「否」。
function modalitiesLabel(m: ModelInfoOut): string {
  if (!m.multimodal || !m.modalities?.length) return '否'
  const cn: Record<string, string> = { image: '图片', video: '视频', audio: '音频' }
  return m.modalities.map((x) => cn[x] ?? x).join('/')
}

// 参数量：亿/十亿友好显示；量化模型标注为打包计数近似（逻辑参数量更高）。
function paramCountLabel(m: ModelInfoOut): string {
  if (!m.param_count) return '--'
  const n = m.param_count
  const val = n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : String(n)
  return m.quantization ? `约 ${val}（量化打包计数）` : `约 ${val}`
}

// 加载状态中文标签：running=已加载、error=加载失败、loading=加载中、其它=未加载。
function loadStatusLabel(s: string): string {
  return { running: '已加载', error: '加载失败', loading: '加载中', unloaded: '未加载' }[s] ?? '未加载'
}

// MoE 显示：是（N 专家 / 激活 K）或 否（Dense 稠密）。
function moeLabel(m: ModelInfoOut): string {
  if (!m.is_moe) return '否（Dense 稠密）'
  const parts: string[] = []
  if (m.num_experts) parts.push(`${m.num_experts} 专家`)
  if (m.num_experts_per_tok) parts.push(`激活 ${m.num_experts_per_tok}`)
  return parts.length ? `是（${parts.join(' / ')}）` : '是'
}

// 上下文类档位（max_model_len / max_num_batched_tokens）的下拉项以「k」显示，
// 如 65536 → 64k、1048576 → 1024k；下拉的 value 仍是原始 token 数，提交不变。
// 其它下拉（kv_cache_dtype / dtype / max_num_seqs 等）保持原样。
const KTOKEN_PARAMS = new Set(['max_model_len', 'max_num_batched_tokens'])
function optionLabel(key: string | number, opt: unknown): string {
  if (KTOKEN_PARAMS.has(String(key)) && typeof opt === 'number' && opt % 1024 === 0) {
    return `${opt / 1024}k`
  }
  return String(opt)
}

function isBoolParam(entry: ParamEntryOut): boolean {
  return typeof entry.default === 'boolean'
}

function isSelectParam(entry: ParamEntryOut): boolean {
  return Array.isArray(entry.options) && entry.options.length > 0
}

// tool_call_parser only makes sense once enable_auto_tool_choice is
// actually checked — the backend marks it editable whenever the model is
// *capable* of tool calls, but whether it's currently relevant also
// depends on this checkbox's live (in-form) value, which the backend has
// no way to know about. So this field's disabled state is computed here
// from the live form value rather than straight from entry.editable.
function isFieldDisabled(key: string, entry: ParamEntryOut): boolean {
  if (!entry.editable) return true
  if (key === 'tool_call_parser') return !formValues.value.enable_auto_tool_choice
  return false
}

// Group params into "basic" (most commonly tuned, expanded by default) vs
// "advanced" (collapsed by default) so a 20+ field flat form doesn't read
// as visual noise. Grouping is presentation-only; nothing here invents or
// renames backend fields — every key still comes straight from paramEntries.
const basicParamKeys = [
  'served_model_name',
  'gpu_memory_utilization',
  'max_model_len',
  'max_num_seqs',
  'max_num_batched_tokens',
  'dtype',
  'quantization',
  'enable_auto_tool_choice',
  'tool_call_parser',
]

const basicParams = computed(() =>
  Object.fromEntries(Object.entries(paramEntries.value).filter(([k]) => basicParamKeys.includes(k))),
)
const advancedParams = computed(() =>
  Object.fromEntries(Object.entries(paramEntries.value).filter(([k]) => !basicParamKeys.includes(k))),
)
</script>

<template>
  <div class="model-config">
    <section class="card">
      <div class="select-header">
        <h2>{{ embedding ? '嵌入式模型选择' : '模型选择' }}</h2>
        <span class="scan-meta">
          上次扫描：{{ scannedAtText }}
        </span>
        <button class="btn secondary" :disabled="modelsStore.rescanning" @click="rescanModels">
          {{ modelsStore.rescanning ? '扫描中...' : '重新扫描模型' }}
        </button>
      </div>
      <p class="scan-note">模型扫描会读取磁盘文件、开销较大，因此改为手动触发；结果会缓存并供全局各页面复用。</p>
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>路径</th>
            <th>大小</th>
            <th>格式</th>
            <th>量化</th>
            <th>多模态</th>
            <th>工具调用</th>
            <th>文件校验</th>
            <th>状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="m in models" :key="m.name">
            <tr :class="{ selected: m.name === selectedModelName, 'row-invalid': !m.valid }">
              <td>{{ m.name }}</td>
              <td class="path-cell">{{ m.path }}</td>
              <td>{{ m.size_gb }} GB</td>
              <td>{{ m.format }}</td>
              <td>{{ m.quantization ?? '--' }}</td>
              <td>{{ modalitiesLabel(m) }}</td>
              <td>{{ m.tool_call_capable ? '支持' : '不支持' }}</td>
              <td>
                <span
                  class="status-dot"
                  :class="m.valid ? 'status-ok' : 'status-error'"
                />
                {{ m.valid ? '正常' : '异常' }}
              </td>
              <td>
                <span
                  class="status-dot"
                  :class="m.load_status === 'running' ? 'status-ok' : m.load_status === 'error' ? 'status-error' : 'status-warning'"
                />
                {{ loadStatusLabel(m.load_status) }}
              </td>
              <td>
                <button class="btn secondary" :disabled="!m.valid" @click="selectedModelName = m.name">选择</button>
              </td>
            </tr>
            <tr v-if="!m.valid" class="row-invalid-detail">
              <td colspan="9">
                <span class="invalid-badge">⚠ 文件不完整/无法加载</span>
                {{ m.validation_errors.join('；') }}
              </td>
            </tr>
          </template>
          <tr v-if="!models.length">
            <td colspan="9" class="muted">未扫描到模型</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="selectedModel" class="card">
      <h2>模型分析 — {{ selectedModel.name }}</h2>
      <div class="analysis-grid">
        <div class="analysis-item"><span class="k">哪种模型</span><span class="v">{{ selectedModel.model_type ?? '--' }}<span v-if="selectedModel.architectures?.length" class="sub"> · {{ selectedModel.architectures.join(', ') }}</span></span></div>
        <div class="analysis-item"><span class="k">参数量</span><span class="v">{{ paramCountLabel(selectedModel) }}</span></div>
        <div class="analysis-item"><span class="k">是否 MoE</span><span class="v">{{ moeLabel(selectedModel) }}</span></div>
        <div class="analysis-item"><span class="k">是否支持图片</span><span class="v">{{ (selectedModel.modalities || []).includes('image') ? '支持' : '否' }}</span></div>
        <div class="analysis-item"><span class="k">多模态</span><span class="v">{{ modalitiesLabel(selectedModel) }}</span></div>
        <div class="analysis-item"><span class="k">工具调用</span><span class="v">{{ selectedModel.tool_call_capable ? '支持' : '不支持' }}</span></div>
        <div class="analysis-item"><span class="k">量化</span><span class="v">{{ selectedModel.quantization ?? (selectedModel.torch_dtype ? '未量化（' + selectedModel.torch_dtype + '）' : '--') }}</span></div>
        <div class="analysis-item"><span class="k">最大上下文</span><span class="v">{{ selectedModel.max_position_embeddings?.toLocaleString() ?? '--' }}</span></div>
        <div class="analysis-item"><span class="k">隐藏维度 / 层数</span><span class="v">{{ selectedModel.hidden_size ?? '--' }} / {{ selectedModel.num_hidden_layers ?? '--' }}</span></div>
        <div class="analysis-item"><span class="k">专家数（总/激活）</span><span class="v">{{ selectedModel.is_moe ? ((selectedModel.num_experts ?? '--') + ' / ' + (selectedModel.num_experts_per_tok ?? '--')) : '—' }}</span></div>
        <div class="analysis-item"><span class="k">格式 / 大小</span><span class="v">{{ selectedModel.format }} · {{ selectedModel.size_gb }} GB</span></div>
        <div class="analysis-item"><span class="k">文件校验</span><span class="v">{{ selectedModel.valid ? '通过' : '未通过：' + (selectedModel.validation_errors || []).join('；') }}</span></div>
      </div>
      <p class="muted analysis-note">参数量取自权重张量计数，量化模型为打包后计数（逻辑参数量更高，仅供参考）。</p>
    </section>

    <section v-if="selectedModel" class="card">
      <h2>参数配置 — {{ selectedModel.name }}（引擎：{{ engine }}）</h2>

      <div class="param-group-title">基础参数</div>
      <div class="param-grid">
        <div v-for="(entry, key) in basicParams" :key="key" class="param-field">
          <label :title="entry.source">{{ key }}</label>

          <input
            v-if="isBoolParam(entry)"
            type="checkbox"
            v-model="formValues[key]"
            :disabled="isFieldDisabled(String(key), entry)"
          />
          <select
            v-else-if="isSelectParam(entry)"
            v-model="formValues[key]"
            :disabled="isFieldDisabled(String(key), entry)"
          >
            <option v-for="opt in entry.options" :key="String(opt)" :value="opt">{{ optionLabel(key, opt) }}</option>
          </select>
          <input
            v-else
            type="text"
            v-model="formValues[key]"
            :disabled="isFieldDisabled(String(key), entry)"
            :placeholder="entry.default === null ? '未设置' : String(entry.default)"
          />
          <span class="param-source">{{ entry.source }}</span>
          <span v-if="String(key) === 'gpu_memory_utilization' && gpuMemHint && gpuMemHint.suggested_max != null" class="gpu-mem-hint">
            💡 当前空闲 {{ gpuMemHint.mem_free_gb }} / 总 {{ gpuMemHint.mem_total_gb }} GiB —
            建议 gpu_memory_utilization ≤ <b>{{ gpuMemHint.suggested_max }}</b>（保守估算，已预留显存开销；仅提醒非强制，实际以启动时显存为准。与其它模型共存时可用更少，可先卸载其它模型）
          </span>
        </div>
      </div>

      <details class="collapsible advanced-params" v-if="Object.keys(advancedParams).length">
        <summary>高级参数（{{ Object.keys(advancedParams).length }} 项）</summary>
        <div class="collapsible-body param-grid">
          <div v-for="(entry, key) in advancedParams" :key="key" class="param-field">
            <label :title="entry.source">{{ key }}</label>

            <input
              v-if="isBoolParam(entry)"
              type="checkbox"
              v-model="formValues[key]"
              :disabled="!entry.editable"
            />
            <select
              v-else-if="isSelectParam(entry)"
              v-model="formValues[key]"
              :disabled="!entry.editable"
            >
              <option v-for="opt in entry.options" :key="String(opt)" :value="opt">{{ optionLabel(key, opt) }}</option>
            </select>
            <input
              v-else
              type="text"
              v-model="formValues[key]"
              :disabled="!entry.editable"
              :placeholder="entry.default === null ? '未设置' : String(entry.default)"
            />
            <span class="param-source">{{ entry.source }}</span>
          </div>
        </div>
      </details>

      <div class="actions-row">
        <button class="btn" :disabled="loading" @click="loadModel">{{ loading ? '加载中...' : '加载模型' }}</button>
        <span class="load-message">{{ loadMessage }}</span>
      </div>
    </section>

    <section class="card">
      <div class="logs-header">
        <h2>运行日志（{{ componentName }}）</h2>
        <span class="muted">实时推送（WebSocket，约 10 秒刷新）</span>
        <button class="btn" @click="wsStore.setLogTarget(componentName, 200)">立即刷新</button>
      </div>
      <pre class="log-box">{{ logs || '暂无日志' }}</pre>
    </section>
  </div>
</template>

<style scoped>
.model-config {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.analysis-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--sp-2) var(--sp-4);
  margin-top: var(--sp-2);
}
.analysis-item {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.06));
  font-size: var(--fs-sm);
}
.analysis-item .k {
  color: var(--text-secondary, #9aa3b2);
  white-space: nowrap;
}
.analysis-item .v {
  color: var(--text, #e4e6eb);
  text-align: right;
  font-weight: 500;
}
.analysis-item .v .sub {
  color: var(--text-secondary, #9aa3b2);
  font-weight: 400;
  font-size: var(--fs-xs);
}
.analysis-note {
  font-size: var(--fs-xs);
  margin-top: var(--sp-2);
}
.select-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.select-header h2 {
  margin: 0;
}
.select-header .btn {
  margin-left: auto;
}
.scan-meta {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.scan-note {
  font-size: var(--fs-xs);
  color: var(--text-faint);
  margin: var(--sp-1) 0 var(--sp-3);
}
.path-cell {
  font-size: var(--fs-xs);
  color: var(--text-faint);
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
tr.selected {
  background: var(--surface-2);
}
tr.row-invalid td {
  background: var(--error-bg);
}
tr.row-invalid-detail td {
  background: var(--error-bg);
  color: var(--error);
  font-size: var(--fs-xs);
  padding-top: 0;
}
.invalid-badge {
  font-weight: 600;
  margin-right: var(--sp-2);
}
.param-group-title {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  font-weight: 600;
  margin-bottom: var(--sp-2);
}
.param-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}
.param-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.param-field label {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.param-source {
  font-size: var(--fs-xs);
  color: var(--text-disabled);
}
.gpu-mem-hint {
  font-size: var(--fs-xs);
  color: var(--warning, #f59e0b);
  line-height: 1.4;
  margin-top: 2px;
}
.gpu-mem-hint b {
  color: var(--warning, #f59e0b);
}
.advanced-params {
  margin-top: var(--sp-3);
}
.actions-row {
  margin-top: var(--sp-4);
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.load-message {
  font-size: var(--fs-base);
  color: var(--info);
}
.logs-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.auto-refresh {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 4px;
}
.log-box {
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
