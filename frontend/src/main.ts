import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { bindAuthToApiClient } from '@/stores/auth'

const app = createApp(App)

app.use(createPinia())
bindAuthToApiClient()
app.use(router)

app.mount('#app')
