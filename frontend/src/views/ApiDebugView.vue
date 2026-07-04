<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import apiClient from '@/api/client'
import type { DebugChatResponse, ModelInfoOut } from '@/types/api'

const generalModels = ref<ModelInfoOut[]>([])
const embeddingModels = ref<ModelInfoOut[]>([])

const selectedModelName = ref('')
const apiFormat = ref<'openai' | 'claude'>('openai')
const system = ref('你是一个严谨、专业的中文 AI 助手，回答简洁准确，必要时分点说明。')
const prompt = ref('你是什么大模型？模型名称？模型参数')
const maxTokens = ref(4096)
const temperature = ref(0.7)
const toolsJson = ref('')
const toolCallParsers = ref<string[]>([])
const embeddingInput = ref('请输入一段文本，测试向量化是否正常返回。')
const showToolParsers = ref(false)

const requestPreview = ref<any>(null)
const responsePreview = ref<DebugChatResponse | null>(null)
const sending = ref(false)
const errorMsg = ref('')

const maxTokenOptions = [2048, 4096, 8192, 16384, 32768, 65536, 81920, 98304, 131072, 163840, 196608, 262144]

// Temperature 预设：不同场景的建议取值，避免用户凭感觉乱填。
const temperatureOptions = [
  { value: 0.0, label: '0.0 · 确定性/代码/抽取（最稳定，可复现）' },
  { value: 0.2, label: '0.2 · 事实问答/RAG（少量随机）' },
  { value: 0.7, label: '0.7 · 通用对话（默认，平衡）' },
  { value: 1.0, label: '1.0 · 创意写作（更发散）' },
  { value: 1.3, label: '1.3 · 头脑风暴/多样性（高随机）' },
]

// 支持的API接口一览：反映 vllm 0.22 容器实际暴露的路由（已在真机
// `vllm serve` 启动日志的 Route 列表中核对过），OpenAI/Claude 两套规范都
// 列出，方便用户照着调试而不用去翻官方文档。仅展示该项目实际接入/调试
// 支持的几个核心接口，不是 vllm 全部路由的穷举。
interface ApiSpecRow {
  seq: number
  type: 'OpenAI' | 'Claude'
  url: string
  purpose: string
  usage: string
}
const apiSpecRows: ApiSpecRow[] = [
  { seq: 1, type: 'OpenAI', url: 'GET /v1/models', purpose: '列出当前模型服务可用的模型', usage: 'Header: Authorization: Bearer <key>' },
  { seq: 2, type: 'OpenAI', url: 'POST /v1/chat/completions', purpose: '对话补全（本页"发送请求"实际调用的接口）', usage: 'body: model, messages[](role/content), max_tokens, temperature, stream, tools' },
  { seq: 3, type: 'OpenAI', url: 'POST /v1/completions', purpose: '纯文本补全（无messages结构，单prompt字符串）', usage: 'body: model, prompt, max_tokens, temperature' },
  { seq: 4, type: 'OpenAI', url: 'POST /v1/embeddings', purpose: '文本向量化，仅嵌入模型支持', usage: 'body: model, input(string或array)' },
  { seq: 5, type: 'Claude', url: 'POST /v1/messages', purpose: 'Anthropic Messages接口（vllm 0.22原生支持）', usage: '注意vllm用 Authorization: Bearer <key> 鉴权(非x-api-key)；body: model, system(顶层字段), messages[], max_tokens' },
  { seq: 6, type: 'Claude', url: 'POST /v1/messages/count_tokens', purpose: '统计消息的token数量（不实际生成）', usage: 'body结构与/v1/messages相同，无需max_tokens' },
]

// OpenAI 与 Claude 对 system 提示词的处理方式不同——容易踩坑，所以单独
// 在页面上提示一下：OpenAI 是把 system 塞进 messages 数组里(role=system)，
// Claude 是 messages 数组之外的顶层字段。
const systemFieldNote: Record<'openai' | 'claude', string> = {
  openai: 'OpenAI 规范：system 作为 messages 数组中的一条消息（{"role":"system","content":...}），不是顶层字段。',
  claude: 'Claude 规范：system 是请求体的顶层字段（与 messages 平级），不放进 messages 数组里。',
}

const loadedModels = computed(() => [...generalModels.value, ...embeddingModels.value].filter((m) => m.load_status === 'running'))

// 选中模型是否为嵌入模型——决定本页展示对话测试还是向量化测试。
// 对嵌入/reranker模型发 /v1/chat/completions 是无意义的（没有生成头，
// vllm 会直接报错），所以必须切换到 /v1/embeddings 的测试方式。
const selectedIsEmbedding = computed(
  () => loadedModels.value.find((m) => m.name === selectedModelName.value)?.is_embedding ?? false,
)

onMounted(async () => {
  const [g, e, parsers] = await Promise.all([
    apiClient.get('/api/models', { params: { type: 'general' } }),
    apiClient.get('/api/models', { params: { type: 'embedding' } }),
    apiClient.get('/api/models/tool-call-parsers'),
  ])
  generalModels.value = g.data.models
  embeddingModels.value = e.data.models
  toolCallParsers.value = parsers.data.tool_call_parsers
  if (loadedModels.value.length) {
    selectedModelName.value = loadedModels.value[0].name
  }
})

async function sendDebugRequest(): Promise<void> {
  errorMsg.value = ''
  sending.value = true
  let extra: Record<string, any> = {}
  if (toolsJson.value.trim()) {
    try {
      extra = { tools: JSON.parse(toolsJson.value) }
    } catch {
      errorMsg.value = '工具调用 JSON 格式不正确'
      sending.value = false
      return
    }
  }

  const payload = {
    model_name: selectedModelName.value,
    api_format: apiFormat.value,
    system: system.value || null,
    prompt: prompt.value,
    max_tokens: maxTokens.value,
    temperature: temperature.value,
    extra,
  }

  try {
    const { data } = await apiClient.post<DebugChatResponse>('/api/debug/chat', payload)
    requestPreview.value = data.request_payload
    responsePreview.value = data
  } catch (err: any) {
    errorMsg.value = err?.response?.data?.detail || '请求发送失败'
  } finally {
    sending.value = false
  }
}

async function sendEmbeddingRequest(): Promise<void> {
  errorMsg.value = ''
  sending.value = true
  try {
    const { data } = await apiClient.post<DebugChatResponse>('/api/debug/embedding', {
      model_name: selectedModelName.value,
      input: embeddingInput.value,
    })
    requestPreview.value = data.request_payload
    responsePreview.value = data
  } catch (err: any) {
    errorMsg.value = err?.response?.data?.detail || '请求发送失败'
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="api-debug">
    <section class="card">
      <h2>请求配置</h2>

      <div class="form-grid">
        <label>
          选择已加载模型
          <select v-model="selectedModelName">
            <option v-for="m in loadedModels" :key="m.name" :value="m.name">
              {{ m.name }}{{ m.is_embedding ? '（嵌入）' : '' }}
            </option>
          </select>
        </label>
      </div>

      <!-- 嵌入模型：向量化测试（对话补全对嵌入模型无意义） -->
      <template v-if="selectedIsEmbedding">
        <p class="mode-note">当前为嵌入模型，使用 /v1/embeddings 向量化测试（对嵌入模型发对话请求无效）。</p>
        <div class="form-grid">
          <label class="full-width">
            待向量化文本（input）
            <textarea v-model="embeddingInput" rows="3"></textarea>
          </label>
        </div>
        <div class="actions-row">
          <button class="btn" :disabled="sending || !selectedModelName" @click="sendEmbeddingRequest">
            {{ sending ? '测试中...' : '测试向量化' }}
          </button>
          <span v-if="errorMsg" class="error-text">{{ errorMsg }}</span>
        </div>
      </template>

      <!-- 通用模型：对话补全测试 -->
      <template v-else>
        <div class="param-group-title">基础参数（常用项，无需展开）</div>
        <div class="form-grid">
          <label>
            API 格式
            <select v-model="apiFormat">
              <option value="openai">OpenAI</option>
              <option value="claude">Claude</option>
            </select>
          </label>
          <label>
            Max Tokens
            <select v-model.number="maxTokens">
              <option v-for="opt in maxTokenOptions" :key="opt" :value="opt">{{ opt }}</option>
            </select>
          </label>
          <label>
            Temperature（按场景选择）
            <select v-model.number="temperature">
              <option v-for="opt in temperatureOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
          </label>
          <label class="full-width">
            System
            <textarea v-model="system" rows="2"></textarea>
            <span class="param-source">{{ systemFieldNote[apiFormat] }}</span>
          </label>
          <label class="full-width">
            Prompt
            <textarea v-model="prompt" rows="4"></textarea>
          </label>
        </div>

        <details class="collapsible advanced-params">
          <summary>高级参数（工具调用 tools）</summary>
          <div class="collapsible-body form-grid">
            <label class="full-width">
              工具调用（tools，JSON 数组，可选）
              <textarea v-model="toolsJson" rows="3" placeholder='[{"type":"function","function":{...}}]'></textarea>
            </label>
            <div class="full-width tool-parser-hint">
              <button type="button" class="link-btn" @click="showToolParsers = !showToolParsers">
                {{ showToolParsers ? '收起' : '查看' }} 当前vllm支持的 tool_call_parser 名称
              </button>
              <span class="param-source">仅当填写了 tools 且加载模型时启用了「自动工具选择」才需要关心。</span>
              <div v-if="showToolParsers" class="tag-list">
                <span v-for="p in toolCallParsers" :key="p" class="tag">{{ p }}</span>
              </div>
            </div>
          </div>
        </details>

        <div class="actions-row">
          <button class="btn" :disabled="sending || !selectedModelName" @click="sendDebugRequest">
            {{ sending ? '发送中...' : '发送请求' }}
          </button>
          <span v-if="errorMsg" class="error-text">{{ errorMsg }}</span>
        </div>
      </template>
    </section>

    <section class="card packet-row">
      <div class="packet">
        <h2>HTTP 请求包</h2>
        <pre class="packet-box">{{ requestPreview ? JSON.stringify(requestPreview, null, 2) : '尚未发送请求' }}</pre>
      </div>
      <div class="packet">
        <h2>HTTP 响应包</h2>
        <pre class="packet-box">{{ responsePreview ? JSON.stringify(responsePreview, null, 2) : '尚未收到响应' }}</pre>
      </div>
    </section>

    <!-- 支持的 API 接口一览：OpenAI/Claude 两套规范的核心接口、用途、调用方式 -->
    <section class="card">
      <h2>支持的 API 接口</h2>
      <table>
        <thead>
          <tr>
            <th>序列</th>
            <th>API 类型</th>
            <th>API URL</th>
            <th>API 功能</th>
            <th>调用方式 / 重要参数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in apiSpecRows" :key="row.seq">
            <td>{{ row.seq }}</td>
            <td>{{ row.type }}</td>
            <td class="api-url">{{ row.url }}</td>
            <td>{{ row.purpose }}</td>
            <td class="api-usage">{{ row.usage }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.api-debug {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.param-group-title {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  font-weight: 600;
  margin-bottom: var(--sp-2);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
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
}
.error-text {
  color: var(--error);
  font-size: var(--fs-sm);
}
.tool-parser-hint {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.mode-note {
  font-size: var(--fs-sm);
  color: var(--info);
  margin: var(--sp-2) 0;
}
.link-btn {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0;
  font-size: var(--fs-sm);
  text-align: left;
}
.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.tag {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
}
.packet-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--sp-3);
}
.packet-box {
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
  font-size: var(--fs-xs);
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
}
.api-url {
  font-family: monospace;
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.api-usage {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
</style>
