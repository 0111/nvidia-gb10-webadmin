import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'overview',
    component: () => import('@/views/OverviewView.vue'),
  },
  {
    path: '/general-model',
    name: 'general-model',
    component: () => import('@/views/GeneralModelView.vue'),
  },
  {
    path: '/embedding-model',
    name: 'embedding-model',
    component: () => import('@/views/EmbeddingModelView.vue'),
  },
  {
    path: '/observability',
    name: 'observability',
    component: () => import('@/views/ObservabilityView.vue'),
  },
  {
    path: '/api-debug',
    name: 'api-debug',
    component: () => import('@/views/ApiDebugView.vue'),
  },
  {
    path: '/component-logs',
    name: 'component-logs',
    component: () => import('@/views/ComponentLogsView.vue'),
  },
  {
    path: '/performance-test',
    name: 'performance-test',
    component: () => import('@/views/PerformanceTestView.vue'),
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Global login guard: every route except /login requires a token.
router.beforeEach((to) => {
  const authStore = useAuthStore()
  if (!to.meta.public && !authStore.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && authStore.isAuthenticated) {
    return { name: 'overview' }
  }
  return true
})

export default router
