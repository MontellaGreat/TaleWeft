import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'

import installNaiveComponents from './plugins/naive'

// 样式
import './assets/styles/main.css'
import './assets/styles/tokens-layout.css'

// Tauri API 初始化（动态端口、环境检测）
import { initApiClient } from './api/config'
import { loadBrowserSession } from './api/session'
import { installGlobalFeedbackIncidentCapture } from './support/feedbackGlobalInstall'

async function bootstrap() {
  // The launcher capability arrives in the URL fragment. Consume it before
  // Vue Router snapshots the initial location, or the router can put the
  // sensitive fragment back into the address bar during its first navigation.
  loadBrowserSession()
  const { default: router } = await import('./router')

  const app = createApp(App)
  installGlobalFeedbackIncidentCapture(app)

  app.use(createPinia())
  app.use(router)
  app.use(installNaiveComponents)

  // Tauri 下须先拿到真实端口再挂路由，否则首屏请求会打到错误 origin（抽屉/广场像「没连上库」）
  try {
    await initApiClient()
  } catch (err) {
    console.warn('[Init] API 客户端初始化失败（可稍后重试）:', err)
  }

  app.mount('#app')
}

void bootstrap()
