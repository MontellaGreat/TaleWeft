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
import NovelSetupGuide from '../../../src/components/onboarding/NovelSetupGuide.vue'

// Fixture capability: never a production token, only accepted by intercepted HTTP.
setSessionToken('browser-fixture-session-token-32-characters')
const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/book/:slug/chapter/:id', component: Chapter },
  { path: '/review', component: defineComponent(() => () => h(AntiAIRewriteReview, { novelId: 'book' })) },
  { path: '/dashboard/:slug', component: AntiAIDashboard },
  { path: '/stream', component: StreamHarness },
  { path: '/setup', component: defineComponent(() => () => h(NovelSetupGuide, { novelId: 'book', show: true })) },
] })
const Root = defineComponent(() => () => h(NConfigProvider, null, {
  default: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
  }),
}))
const app = createApp(Root).use(createPinia()).use(router).use(installNaiveComponents)
await router.isReady()
app.mount('#app')
