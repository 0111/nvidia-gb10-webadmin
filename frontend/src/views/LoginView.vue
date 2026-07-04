<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

async function handleSubmit(): Promise<void> {
  error.value = ''
  loading.value = true
  try {
    await authStore.login({ username: username.value, password: password.value })
    const redirect = (route.query.redirect as string) || '/'
    router.push(redirect)
  } catch (err: any) {
    error.value = err?.response?.data?.detail || '登录失败，请检查用户名和密码'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="handleSubmit">
      <h1>NVIDIA GB10 Manager</h1>
      <p class="subtitle">本地大模型管理工具 · 登录</p>
      <label>
        用户名
        <input v-model="username" type="text" autocomplete="username" required />
      </label>
      <label>
        密码
        <input v-model="password" type="password" autocomplete="current-password" required />
      </label>
      <p v-if="error" class="error">{{ error }}</p>
      <button class="btn" type="submit" :disabled="loading">
        {{ loading ? '登录中...' : '登录' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--bg);
}
.login-card {
  width: 320px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-6);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
h1 {
  font-size: var(--fs-xl);
  margin: 0;
  color: var(--text);
}
.subtitle {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-base);
  color: var(--text-muted);
}
input {
  width: 100%;
}
.error {
  color: var(--error);
  font-size: var(--fs-sm);
  margin: 0;
}
button {
  margin-top: var(--sp-2);
}
</style>
