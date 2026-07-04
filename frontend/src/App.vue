<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useWsStore } from '@/stores/ws'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const wsStore = useWsStore()

const navLinks = [
  { name: 'overview', label: '实时总览' },
  { name: 'general-model', label: '通用大模型配置' },
  { name: 'embedding-model', label: '嵌入式模型配置' },
  { name: 'observability', label: '运行观测' },
  { name: 'api-debug', label: 'API调试' },
  { name: 'component-logs', label: '组件日志' },
  { name: 'performance-test', label: '性能测试' },
  { name: 'settings', label: '高级设置' },
]

const showLayout = computed(() => route.name !== 'login')

// Open the single shared WebSocket connection once we have a token, and
// whenever the token changes (e.g. fresh login). Closed implicitly when
// the token is cleared (logout) since reconnect checks localStorage.
watch(
  () => authStore.token,
  (token) => {
    if (token) {
      wsStore.connect(token)
    } else {
      wsStore.disconnect()
    }
  },
  { immediate: true },
)

function logout(): void {
  authStore.logout()
  wsStore.disconnect()
  router.push({ name: 'login' })
}
</script>

<template>
  <div v-if="showLayout" class="app-shell">
    <header class="topbar">
      <div class="brand">NVIDIA GB10 Manager</div>
      <nav class="nav">
        <RouterLink
          v-for="link in navLinks"
          :key="link.name"
          :to="{ name: link.name }"
          class="nav-link"
          :class="{ active: route.name === link.name }"
        >
          {{ link.label }}
        </RouterLink>
      </nav>
      <div class="actions">
        <span class="ws-indicator" :class="{ on: wsStore.connected }" title="WebSocket 连接状态" />
        <button class="logout-btn" @click="logout">退出登录</button>
      </div>
    </header>
    <main class="content">
      <RouterView />
    </main>
  </div>
  <RouterView v-else />
</template>

<style>
:root {
  /* Spacing scale */
  --sp-1: 4px;
  --sp-2: 8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-5: 20px;
  --sp-6: 24px;

  /* Font scale */
  --fs-xs: 11px;
  --fs-sm: 12px;
  --fs-base: 13px;
  --fs-md: 14px;
  --fs-lg: 16px;
  --fs-xl: 20px;
  --fs-xxl: 26px;

  /* Surfaces */
  --bg: #0f1115;
  --surface: #161a22;
  --surface-2: #1a1e27;
  --surface-3: #0b0d12;
  --border: #2a2f3a;
  --border-strong: #3a3f4a;

  /* Text */
  --text: #e4e6eb;
  --text-muted: #9aa3b2;
  --text-faint: #6b7280;
  --text-disabled: #4b5563;

  /* Brand / actions */
  --accent: #2563eb;
  --accent-hover: #1d4ed8;
  --info: #60a5fa;

  /* Semantic status colors: green=ok, yellow=warning, red=error/danger */
  --ok: #22c55e;
  --ok-bg: rgba(34, 197, 94, 0.12);
  --warning: #f59e0b;
  --warning-bg: rgba(245, 158, 11, 0.12);
  --error: #ef4444;
  --error-bg: rgba(239, 68, 68, 0.12);

  --radius: 6px;
  --radius-sm: 4px;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: var(--fs-base);
}
.app-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
.topbar {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  padding: 0 var(--sp-4);
  height: 48px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.brand {
  font-weight: 600;
  font-size: var(--fs-md);
  white-space: nowrap;
}
.nav {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  flex: 1;
}
.nav-link {
  color: var(--text-muted);
  text-decoration: none;
  font-size: var(--fs-base);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
}
.nav-link:hover {
  background: #20242e;
  color: #fff;
}
.nav-link.active {
  background: var(--accent);
  color: #fff;
}
.actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.ws-indicator {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--text-faint);
  display: inline-block;
}
.ws-indicator.on {
  background: var(--ok);
}
.logout-btn {
  background: var(--border);
  color: var(--text);
  border: none;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.logout-btn:hover {
  background: var(--border-strong);
}
.content {
  flex: 1;
  padding: var(--sp-4);
}
table {
  border-collapse: collapse;
  width: 100%;
}
th, td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-base);
}
th {
  color: var(--text-muted);
  font-weight: 500;
  font-size: var(--fs-sm);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
input, select, textarea, button {
  font-family: inherit;
}
input[type='text'], input[type='number'], input[type='password'], select, textarea {
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  font-size: var(--fs-base);
}
input:focus, select:focus, textarea:focus {
  outline: none;
  border-color: var(--accent);
}
button.btn {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  padding: 6px 14px;
  font-size: var(--fs-base);
  cursor: pointer;
  transition: background 0.15s ease;
}
button.btn:hover {
  background: var(--accent-hover);
}
button.btn.secondary {
  background: var(--border);
  color: var(--text);
}
button.btn.secondary:hover {
  background: var(--border-strong);
}
button.btn.danger {
  background: var(--error);
}
button.btn.danger:hover {
  background: #c0392b;
}
button.btn.ghost {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-muted);
}
button.btn.ghost:hover {
  border-color: var(--border-strong);
  color: var(--text);
}
button.btn:disabled {
  background: var(--border-strong);
  color: var(--text-faint);
  cursor: not-allowed;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-4);
}
.card h2 {
  font-size: var(--fs-md);
  margin: 0 0 var(--sp-3);
  color: var(--text-muted);
  font-weight: 600;
}
.status-ok { color: var(--ok); }
.status-warning { color: var(--warning); }
.status-error { color: var(--error); }
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
}
.status-dot.status-ok { background: var(--ok); }
.status-dot.status-warning { background: var(--warning); }
.status-dot.status-error { background: var(--error); }
.muted {
  color: var(--text-faint);
  font-size: var(--fs-sm);
}
.section-divider {
  border: none;
  border-top: 1px solid var(--border);
  margin: var(--sp-4) 0;
}
details.collapsible {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--sp-2) var(--sp-3);
  background: var(--surface-2);
}
details.collapsible > summary {
  cursor: pointer;
  font-size: var(--fs-sm);
  color: var(--text-muted);
  font-weight: 600;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
details.collapsible > summary::-webkit-details-marker {
  display: none;
}
details.collapsible > summary::before {
  content: '\25B8';
  display: inline-block;
  transition: transform 0.15s ease;
  font-size: 10px;
}
details.collapsible[open] > summary::before {
  transform: rotate(90deg);
}
details.collapsible .collapsible-body {
  margin-top: var(--sp-3);
}

/* ---------------------------------------------------------------------------
   Responsive: phones (≤640px) and tablets in portrait (≤900px).
   Main fixes for "手机/iPad 上有些地方被压缩":
   - 宽表格改为横向滚动而不是把列挤变形（display:block + overflow-x:auto +
     nowrap 是让 <table> 在窄屏可横滑的标准做法）。
   - 顶栏导航横向滚动而不是换行堆高。
   - 内容区与卡片内边距收窄，给内容更多宽度。
--------------------------------------------------------------------------- */
@media (max-width: 900px) {
  table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
    -webkit-overflow-scrolling: touch;
  }
}
@media (max-width: 640px) {
  .content {
    padding: var(--sp-2);
  }
  .card {
    padding: var(--sp-3);
  }
  .topbar {
    flex-wrap: nowrap;
    gap: var(--sp-2);
    padding: 0 var(--sp-2);
  }
  .nav {
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .nav-link {
    white-space: nowrap;
    padding: 6px 8px;
  }
  th {
    text-transform: none;
    letter-spacing: 0;
  }
}
</style>
