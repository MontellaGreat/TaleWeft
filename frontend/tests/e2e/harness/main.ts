import { createApp, defineComponent, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory, RouterView } from 'vue-router'
import { NConfigProvider, NMessageProvider, NDialogProvider } from 'naive-ui'
import installNaiveComponents from '../../../src/plugins/naive'
import { setSessionToken } from '../../../src/api/session'
import Chapter from '../../../src/views/Chapter.vue'
import AntiAIRewriteReview from '../../../src/components/workbench/promptPlaza/AntiAIRewriteReview.vue'
import AntiAIDashboard from '../../../src/components/workbench/promptPlaza/AntiAIDashboard.vue'
import StreamHarness from './StreamHarness.vue'
import DAGCanvasHarness from './DAGCanvasHarness.vue'
import NovelSetupGuide from '../../../src/components/onboarding/NovelSetupGuide.vue'
import Home from '../../../src/views/Home.vue'
import SettingsPanel from '../../../src/components/workbench/SettingsPanel.vue'
import '../../../src/assets/styles/main.css'
import '../../../src/assets/styles/tokens-layout.css'

// Fixture capability: never a production token, only accepted by intercepted HTTP.
setSessionToken('browser-fixture-session-token-32-characters')
const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/book/:slug/chapter/:id', component: Chapter },
  { path: '/review', component: defineComponent(() => () => h(AntiAIRewriteReview, { novelId: 'book' })) },
  { path: '/dashboard/:slug', component: AntiAIDashboard },
  { path: '/stream', component: StreamHarness },
  { path: '/dag-canvas', component: DAGCanvasHarness },
  { path: '/setup', component: defineComponent(() => () => h(NovelSetupGuide, { novelId: 'book', show: true })) },
  { path: '/library', component: Home },
  { path: '/support', component: defineComponent(() => () => h('div', { style: 'width:min(420px,100vw);height:calc(100vh - 24px);margin:12px;overflow:hidden' },
    h(SettingsPanel, { slug: 'book', currentPanel: 'bible' }))) },
] })
const Root = defineComponent(() => () => h(NConfigProvider, null, {
  default: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
  }),
}))
const app = createApp(Root).use(createPinia()).use(router).use(installNaiveComponents)
await router.isReady()
app.mount('#app')
