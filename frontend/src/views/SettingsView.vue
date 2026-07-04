<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import apiClient from '@/api/client'
import type {
  SearxngActionResponse,
  SearxngProxyTestResponse,
  SearxngSearchResponse,
  SearxngStatusResponse,
  SettingsOut,
} from '@/types/api'

const settings = ref<SettingsOut | null>(null)
const form = ref<Record<string, any>>({})
const saving = ref(false)
const message = ref('')
const copiedField = ref('')

// SearXNG 状态/代理/搜索调试 —— 已接入阶段四真实后端 API
// （web/routers/searxng_router.py + core.searxng_client / SearxngManager）。
const searxngRunning = ref<boolean | null>(null)
const searxngActionPending = ref(false)
// Bound directly to form.searxng_proxy_url so "保存设置" persists it via
// PUT /api/settings, and a page refresh reloads the saved value via
// GET /api/settings (see onMounted below) instead of resetting to empty.
const searxngProxy = ref('')
const searxngDebugQuery = ref('')
const searxngDebugResult = ref('')

// 真实的 SearXNG 检索 URL：用用户当前访问前端所用的主机名（即局域网可达的 IP）
// + 配置里的 searxng_port，替换原先 {host-IP}:{searxng-Port} 占位符。
// 局域网可访问的 SearXNG 基地址：用用户当前访问前端所用的主机名（即局域网
// 可达 IP）+ 配置里的 searxng_port，替换原先固定的 127.0.0.1（仅本机可达）。
const searxngServiceUrl = computed(() => {
  const host = window.location.hostname || '127.0.0.1'
  const port = settings.value?.searxng_port ?? 8080
  return `http://${host}:${port}`
})
const searxngSearchUrl = computed(
  () => `${searxngServiceUrl.value}/search?q=<query>&format=json&categories=general&language=auto&safesearch=0`,
)

async function refreshSearxngStatus(): Promise<void> {
  try {
    const { data } = await apiClient.get<SearxngStatusResponse>('/api/searxng/status')
    searxngRunning.value = data.running
  } catch {
    searxngRunning.value = null
  }
}

onMounted(async () => {
  const { data } = await apiClient.get<SettingsOut>('/api/settings')
  settings.value = data
  form.value = {
    web_port: data.web_port,
    web_host: data.web_host,
    admin_username: data.admin_username,
    model_root_dir: data.model_root_dir,
    data_dir: data.data_dir,
    searxng_port: data.searxng_port,
    searxng_url: data.searxng_url,
    frontend_port: data.frontend_port,
    vllm_image: data.vllm_image,
    cuda_compat_dir: data.cuda_compat_dir,
    searxng_proxy_url: data.searxng_proxy_url ?? '',
    admin_password: data.admin_password,
    vllm_api_key: data.vllm_api_key,
    rotate_secret_key: false,
  }
  searxngProxy.value = data.searxng_proxy_url ?? ''
  await refreshSearxngStatus()
})

async function saveSettings(): Promise<void> {
  saving.value = true
  message.value = ''
  const payload: Record<string, any> = { ...form.value, searxng_proxy_url: searxngProxy.value || null }
  try {
    const { data } = await apiClient.put<SettingsOut>('/api/settings', payload)
    settings.value = data
    form.value.admin_password = data.admin_password
    form.value.vllm_api_key = data.vllm_api_key
    form.value.searxng_proxy_url = data.searxng_proxy_url ?? ''
    searxngProxy.value = data.searxng_proxy_url ?? ''
    message.value = '设置已保存'
    form.value.rotate_secret_key = false
  } catch (err: any) {
    message.value = err?.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function copyToClipboard(value: string, field: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    copiedField.value = field
    setTimeout(() => {
      if (copiedField.value === field) copiedField.value = ''
    }, 1500)
  } catch {
    message.value = '复制失败，请手动选中复制'
  }
}

async function startSearxng(): Promise<void> {
  searxngActionPending.value = true
  try {
    const { data } = await apiClient.post<SearxngActionResponse>('/api/searxng/start')
    searxngDebugResult.value = data.message
  } catch (err: any) {
    searxngDebugResult.value = err?.response?.data?.detail || 'SearXNG 启动失败'
  } finally {
    searxngActionPending.value = false
    await refreshSearxngStatus()
  }
}

async function stopSearxng(): Promise<void> {
  searxngActionPending.value = true
  try {
    const { data } = await apiClient.post<SearxngActionResponse>('/api/searxng/stop')
    searxngDebugResult.value = data.message
  } catch (err: any) {
    searxngDebugResult.value = err?.response?.data?.detail || 'SearXNG 停止失败'
  } finally {
    searxngActionPending.value = false
    await refreshSearxngStatus()
  }
}

async function testSearxngProxy(): Promise<void> {
  try {
    const { data } = await apiClient.post<SearxngProxyTestResponse>('/api/searxng/proxy/test', {
      proxy: searxngProxy.value || null,
    })
    searxngDebugResult.value = data.ok
      ? `代理可用，延迟 ${data.latency_ms ?? '-'} ms，状态码 ${data.status_code ?? '-'}`
      : `代理测试失败: ${data.error ?? '未知错误'}`
  } catch (err: any) {
    searxngDebugResult.value = err?.response?.data?.detail || '代理测试请求失败'
  }
}

async function testSearxngQuery(): Promise<void> {
  try {
    const { data } = await apiClient.get<SearxngSearchResponse>('/api/searxng/search', {
      params: { q: searxngDebugQuery.value },
    })
    searxngDebugResult.value = data.ok
      ? JSON.stringify(data.data, null, 2)
      : `搜索失败: ${data.error ?? '未知错误'}`
  } catch (err: any) {
    searxngDebugResult.value = err?.response?.data?.detail || '搜索调试请求失败'
  }
}
</script>

<template>
  <div class="settings">
    <!-- 账号安全 -->
    <section class="card">
      <h2>账号安全</h2>
      <p class="muted section-note">
        本工具为内部单管理员场景，密钥/密码以明文展示，不做掩码处理，请妥善保管该页面的访问权限。
      </p>
      <div v-if="settings" class="form-grid">
        <label>
          管理员用户名
          <input type="text" v-model="form.admin_username" />
        </label>
        <label>
          管理员密码（明文）
          <span class="copy-row">
            <input type="text" v-model="form.admin_password" />
            <button class="btn ghost copy-btn" @click="copyToClipboard(form.admin_password, 'admin_password')">
              {{ copiedField === 'admin_password' ? '已复制' : '复制' }}
            </button>
          </span>
        </label>
        <label class="full-width">
          密钥 secret_key（明文）
          <span class="copy-row">
            <input type="text" :value="settings.secret_key" disabled />
            <button class="btn ghost copy-btn" @click="copyToClipboard(settings.secret_key, 'secret_key')">
              {{ copiedField === 'secret_key' ? '已复制' : '复制' }}
            </button>
          </span>
        </label>
        <label>
          <span class="rotate-row">
            <input type="checkbox" v-model="form.rotate_secret_key" />
            轮换密钥（将使所有现有登录会话失效）
          </span>
        </label>
        <label class="full-width">
          vllm API Key（明文，sk-xxxx，所有通用/嵌入模型容器共用，OpenAI/Claude兼容接口鉴权用）
          <span class="copy-row">
            <input type="text" v-model="form.vllm_api_key" />
            <button class="btn ghost copy-btn" @click="copyToClipboard(form.vllm_api_key, 'vllm_api_key')">
              {{ copiedField === 'vllm_api_key' ? '已复制' : '复制' }}
            </button>
          </span>
        </label>
      </div>
    </section>

    <!-- 网络端口 -->
    <section class="card">
      <h2>网络端口</h2>
      <div v-if="settings" class="form-grid">
        <label>
          Web 端口
          <input type="number" v-model.number="form.web_port" />
        </label>
        <label>
          Web Host
          <input type="text" v-model="form.web_host" />
        </label>
        <label>
          SearXNG 端口
          <input type="number" v-model.number="form.searxng_port" />
        </label>
        <label>
          前端端口
          <input type="number" v-model.number="form.frontend_port" />
        </label>
      </div>
    </section>

    <!-- 模型路径 -->
    <section class="card">
      <h2>模型路径</h2>
      <div v-if="settings" class="form-grid">
        <label>
          模型根目录
          <input type="text" v-model="form.model_root_dir" />
        </label>
        <label>
          数据目录
          <input type="text" v-model="form.data_dir" />
        </label>
        <label>
          vLLM 镜像
          <input type="text" v-model="form.vllm_image" />
        </label>
        <label>
          CUDA 兼容包目录
          <input type="text" v-model="form.cuda_compat_dir" />
        </label>
      </div>
      <div class="actions-row">
        <button class="btn" :disabled="saving" @click="saveSettings">保存设置</button>
        <span class="message">{{ message }}</span>
      </div>
    </section>

    <!-- SearXNG -->
    <section class="card">
      <h2>SearXNG</h2>
      <div class="actions-row">
        <span class="message">
          <span
            class="status-dot"
            :class="searxngRunning === null ? 'status-warning' : searxngRunning ? 'status-ok' : 'status-error'"
          />
          状态：
          <template v-if="searxngRunning === null">未知</template>
          <template v-else-if="searxngRunning">运行中</template>
          <template v-else>未运行</template>
        </span>
        <button class="btn secondary" :disabled="searxngActionPending" @click="startSearxng">启动</button>
        <button class="btn danger" :disabled="searxngActionPending" @click="stopSearxng">停止</button>
        <button class="btn ghost" @click="refreshSearxngStatus">刷新状态</button>
      </div>

      <hr class="section-divider" />

      <div class="form-grid">
        <label>
          代理地址（留空表示不使用代理，保存后将持久化并用于真实搜索请求）
          <input type="text" v-model="searxngProxy" placeholder="例如 http://127.0.0.1:7890" />
        </label>
        <label>
          SearXNG 服务地址（局域网可访问）
          <input type="text" :value="searxngServiceUrl" disabled />
          <span class="muted">后端内部仍以 {{ settings?.searxng_url }} 调用；此处展示供其它设备访问的局域网地址。</span>
        </label>
      </div>
      <div class="actions-row">
        <button class="btn secondary" @click="testSearxngProxy">测试代理</button>
        <button class="btn" :disabled="saving" @click="saveSettings">保存代理设置</button>
      </div>

      <hr class="section-divider" />

      <p class="muted">
        检索 URL：<code>{{ searxngSearchUrl }}</code>
      </p>
      <div class="form-grid">
        <label class="full-width">
          调试查询词
          <input type="text" v-model="searxngDebugQuery" />
        </label>
      </div>
      <div class="actions-row">
        <button class="btn secondary" @click="testSearxngQuery">执行搜索调试</button>
      </div>
      <pre v-if="searxngDebugResult" class="result-box">{{ searxngDebugResult }}</pre>
    </section>
  </div>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.section-note {
  margin: 0 0 var(--sp-3);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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
.copy-row {
  display: flex;
  gap: var(--sp-2);
}
.copy-row input {
  flex: 1;
}
.copy-btn {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  padding: 5px 10px;
}
.rotate-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.actions-row {
  margin-top: var(--sp-3);
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.message {
  font-size: var(--fs-sm);
  color: var(--info);
  display: flex;
  align-items: center;
}
.muted {
  color: var(--text-faint);
  font-size: var(--fs-sm);
}
.result-box {
  margin-top: var(--sp-3);
  background: var(--surface-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
}
</style>
