import { createApp, defineComponent, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { NConfigProvider, NMessageProvider, NDialogProvider } from 'naive-ui'
import installNaiveComponents from '../../../src/plugins/naive'
import { setSessionToken } from '../../../src/api/session'
import CharacterDialoguePanel from '../../../src/components/workbench/CharacterDialoguePanel.vue'
import '../../../src/assets/styles/main.css'
import '../../../src/assets/styles/tokens-layout.css'

setSessionToken('browser-fixture-session-token-32-characters')
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: defineComponent(() => () =>
  h('main', { style: 'height: 900px; max-height: 95vh' }, [h(CharacterDialoguePanel, { slug: 'book', currentChapterNumber: 1 })])) }] })
const Root = defineComponent(() => () => h(NConfigProvider, null, { default: () => h(NMessageProvider, null, {
  default: () => h(NDialogProvider, null, { default: () => h(RouterView) }),
}) }))
const app = createApp(Root).use(createPinia()).use(router).use(installNaiveComponents)
await router.push('/')
await router.isReady()
app.mount('#app')
