import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Home from '../src/views/Home.vue'
import MarketTaxonomyPicker from '../src/components/taxonomy/MarketTaxonomyPicker.vue'
import { MARKET_PROFILE_DIMENSIONS } from '../src/domain/taxonomy/cnMarketV2'

const mocks = vi.hoisted(() => ({
  create: vi.fn(), list: vi.fn(async () => []), message: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
  readiness: vi.fn(),
}))
vi.mock('../src/api/novel', () => ({ novelApi: { createNovel: mocks.create, listNovels: mocks.list } }))
vi.mock('../src/api/config', () => ({ apiClient: { get: mocks.readiness } }))
vi.mock('/taleweft-mark.svg', () => ({ default: '/taleweft-mark.svg' }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('naive-ui', async (importOriginal) => ({
  ...await importOriginal<typeof import('naive-ui')>(),
  useMessage: () => mocks.message,
  NIcon: { render: () => null },
}))
vi.mock('../src/stores/statsStore', () => ({ useStatsStore: () => ({ loadGlobalStats: vi.fn() }) }))
vi.mock('../src/stores/appSettingsShellStore', () => ({ useAppSettingsShellStore: () => ({ open: vi.fn() }) }))
vi.mock('../src/components/global/GlobalLLMEntryButton.vue', () => ({ default: {
  setup(_props: unknown, { expose }: { expose: (value: unknown) => void }) {
    const tab = ref('')
    expose({ openPanel: (value: string) => { tab.value = value } })
    return () => tab.value ? h('div', { 'data-testid': 'model-panel' }, `模型控制台：${tab.value}`) : null
  },
} }))
vi.mock('../src/components/global/PromptPlazaEntryButton.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/components/stats/StatsSidebar.vue', () => ({ default: {
  emits: ['create-book'],
  setup(_props: unknown, { emit }: { emit: (event: string) => void }) {
    return () => h('button', { onClick: () => emit('create-book') }, '侧栏新建')
  },
} }))
vi.mock('../src/components/onboarding/NovelSetupGuide.vue', () => ({ default: { render: () => null } }))

const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value', 'disabled'], emits: ['update:value'], setup(props, { emit, expose }) {
  const element = ref<HTMLTextAreaElement>()
  expose({ focus: () => element.value?.focus() })
  return () => h('textarea', { ref: element, value: props.value, disabled: props.disabled, onInput: (event: Event) => emit('update:value', (event.target as HTMLTextAreaElement).value) })
} })
const Checkbox = defineComponent({ props: ['checked', 'disabled'], emits: ['update:checked'], setup(props, { emit }) {
  return () => h('input', { type: 'checkbox', checked: props.checked, disabled: props.disabled,
    onChange: (event: Event) => emit('update:checked', (event.target as HTMLInputElement).checked) })
} })
const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', slots.default?.()) } })
const Modal = defineComponent({ props: ['show', 'title'], setup(props, { slots }) {
  return () => props.show ? h('div', { role: 'dialog' }, [h('h2', props.title), slots.default?.(), slots.action?.()]) : null
} })
const wrappers: VueWrapper[] = []
const readyModels = { ready: true, llm: { ready: true, reasons: [] }, embedding: { ready: true, reasons: [], mode: 'local' } }
beforeEach(() => { localStorage.clear(); mocks.readiness.mockReset(); mocks.readiness.mockResolvedValue(readyModels) })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.clearAllMocks() })

async function home(openCreation = true) {
  mocks.create.mockResolvedValue({ id: 'created', target_chapters: 100 })
  const stubs: Record<string, unknown> = {
    NButton: Button, NInput: Input, NCheckbox: Checkbox, NSelect: true, NModal: Modal, NDrawer: Modal, NDrawerContent: Box, NSteps: Box, NStep: true,
    NovelSetupGuide: true, MarketTaxonomyPicker,
  }
  for (const name of ['NCard', 'NSpace', 'NIcon', 'NRadioGroup', 'NRadio', 'NAlert', 'NGrid', 'NGi', 'NFormItem', 'NEmpty', 'NTag', 'NInputNumber', 'NSpin', 'NPopconfirm']) stubs[name] = Box
  const wrapper = mount(Home, { global: { stubs } })
  wrappers.push(wrapper)
  await flushPromises()
  if (openCreation) {
    await button(wrapper, '＋ 新建书目').trigger('click')
    await flushPromises()
    await wrapper.get('textarea.premise-input').setValue('测试梗概：主角寻找改变生活的机会。')
  }
  return wrapper
}

function button(wrapper: VueWrapper, label: string) {
  return wrapper.findAll('button').find(item => item.text() === label)!
}

async function nextStep(wrapper: VueWrapper) {
  await button(wrapper, '下一步').trigger('click')
}

function createButton(wrapper: VueWrapper) {
  return wrapper.findAll('button').find((item) => item.text().includes('建档并进入工作台'))!
}

describe('Home sends the actual market selection to the backend', () => {
  it('sends canonical provenance, audience, and all five final reviewed dimensions', async () => {
    const wrapper = await home()
    await nextStep(wrapper)
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.xuanhuan"]').trigger('click')
    await wrapper.get('[data-testid="market-theme-theme.xuanhuan.dongfang"]').trigger('click')
    await nextStep(wrapper)
    expect(createButton(wrapper).attributes('disabled')).toBeDefined()
    await button(wrapper, '上一步').trigger('click')
    await wrapper.get('[data-testid="market-profile-world_tone"]').setValue('  本书的最终世界观  ')
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await nextStep(wrapper)
    await createButton(wrapper).trigger('click')
    await flushPromises()
    expect(mocks.create).toHaveBeenCalledTimes(1)
    const payload = mocks.create.mock.calls[0]![0]
    expect(payload.audience_mode).toBe('male')
    expect(payload.canonical_genre_id).toBe('major.xuanhuan')
    expect(payload.canonical_theme_id).toBe('theme.xuanhuan.dongfang')
    expect(payload.taxonomy_version).toMatch(/^cn_market_v2_/)
    expect(payload.world_preset).toBe('本书的最终世界观')
    const snapshot = payload.custom_market_profile.custom_theme.profile
    expect(snapshot.world_tone).toBe(payload.world_preset)
    for (const dimension of MARKET_PROFILE_DIMENSIONS.slice(1)) expect(snapshot[dimension]).toBe(payload[dimension])
    expect(payload.custom_market_profile.profile_review_status).toBe('user_confirmed')
    expect(payload).not.toHaveProperty('profile_snapshot_hash')
  })

  it('sends a canonical category plus custom theme and refuses to reuse confirmation after editing', async () => {
    const wrapper = await home()
    await nextStep(wrapper)
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.xuanhuan"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('我自己的世界主题')
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await wrapper.get('[data-testid="market-profile-writing_style"]').setValue('新的文风')
    await nextStep(wrapper)
    expect(createButton(wrapper).attributes('disabled')).toBeDefined()
    await button(wrapper, '上一步').trigger('click')
    expect(mocks.create).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await nextStep(wrapper)
    await createButton(wrapper).trigger('click')
    await flushPromises()
    const payload = mocks.create.mock.calls[0]![0]
    expect(payload.canonical_genre_id).toBe('major.xuanhuan')
    expect(payload.canonical_theme_id).toBe('')
    expect(payload.custom_market_profile.custom_theme.label).toBe('我自己的世界主题')
    expect(payload.custom_market_profile.custom_theme.parent_canonical_category_id).toBe('major.xuanhuan')
    expect(payload.writing_style).toBe('新的文风')
    expect(payload.custom_market_profile.custom_theme.profile.writing_style).toBe('新的文风')
  })

  it('retains a 10000-character premise and selected profile when returning through creation steps', async () => {
    const wrapper = await home()
    const premise = '长篇故事'.repeat(2500)
    await wrapper.get('textarea.premise-input').setValue(premise)
    await nextStep(wrapper)
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.xuanhuan"]').trigger('click')
    await wrapper.get('[data-testid="market-theme-theme.xuanhuan.dongfang"]').trigger('click')
    await wrapper.get('[data-testid="market-profile-world_tone"]').setValue('保留的世界设定')
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await nextStep(wrapper)
    expect(wrapper.get('.creation-summary').text()).toContain('10000 / 10000')
    await button(wrapper, '上一步').trigger('click')
    expect((wrapper.get('[data-testid="market-profile-world_tone"]').element as HTMLTextAreaElement).value).toBe('保留的世界设定')
    await button(wrapper, '上一步').trigger('click')
    expect((wrapper.get('textarea.premise-input').element as HTMLTextAreaElement).value).toBe(premise)
    expect(mocks.create).not.toHaveBeenCalled()
    await nextStep(wrapper)
    await nextStep(wrapper)
    await createButton(wrapper).trigger('click')
    await flushPromises()
    expect(mocks.create.mock.calls[0]![0].premise).toBe(premise)
    expect(mocks.create.mock.calls[0]![0].world_preset).toBe('保留的世界设定')
  })

})

describe('Home requires saved language and embedding model configuration before creation', () => {
  it.each(['＋ 新建书目', '创建第一本书', '侧栏新建'])('blocks %s and opens the language model console only after confirmation', async entry => {
    mocks.readiness.mockResolvedValue({ ...readyModels, ready: false, llm: { ready: false, reasons: ['未配置语言模型'] } })
    const wrapper = await home(false)
    if (entry === '侧栏新建') await button(wrapper, '统计与工具').trigger('click')
    await button(wrapper, entry).trigger('click')
    await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
    expect(wrapper.text()).toContain('请先配置好模型')
    expect(wrapper.find('[data-testid="model-panel"]').exists()).toBe(false)
    expect(mocks.create).not.toHaveBeenCalled()
    await button(wrapper, '打开模型控制台').trigger('click')
    expect(wrapper.get('[data-testid="model-panel"]').text()).toContain('llm')
    expect(wrapper.text()).not.toContain('请先配置好模型')
  })

  it('directs a missing embedding configuration to the embedding tab and retains the existing draft', async () => {
    localStorage.setItem('taleweft-new-book-draft', JSON.stringify({ book: { premise: '不能丢失的故事草稿' } }))
    mocks.readiness.mockResolvedValue({ ...readyModels, ready: false, embedding: { ready: false, reasons: ['本地嵌入资源缺失'], mode: 'local' } })
    const wrapper = await home(false)
    await button(wrapper, '创建第一本书').trigger('click'); await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
    await button(wrapper, '打开模型控制台').trigger('click')
    expect(wrapper.get('[data-testid="model-panel"]').text()).toContain('embedding')
    expect(localStorage.getItem('taleweft-new-book-draft')).toContain('不能丢失的故事草稿')
    mocks.readiness.mockResolvedValue(readyModels)
    await button(wrapper, '＋ 新建书目').trigger('click'); await flushPromises()
    expect((wrapper.get('textarea.premise-input').element as HTMLTextAreaElement).value).toBe('不能丢失的故事草稿')
  })

  it('deduplicates repeated entry clicks while the readiness request is pending', async () => {
    let resolve!: (value: unknown) => void
    mocks.readiness.mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = await home(false)
    await button(wrapper, '创建第一本书').trigger('click')
    await button(wrapper, '＋ 新建书目').trigger('click')
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
    expect(mocks.readiness).toHaveBeenCalledTimes(1)
    expect(mocks.readiness).toHaveBeenCalledWith('/settings/model-readiness')
    resolve(readyModels); await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(true)
  })

  it('shows distinct missing language and embedding requirements without repeating reasons', async () => {
    mocks.readiness.mockResolvedValue({ ready: false,
      llm: { ready: false, reasons: ['缺少语言模型 API Key', '缺少语言模型 API Key', '缺少语言模型名称'] },
      embedding: { ready: false, reasons: ['本地模型文件不存在', ' 本地模型文件不存在 '], mode: 'local' },
    })
    const wrapper = await home(false)
    await button(wrapper, '创建第一本书').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('语言模型')
    expect(wrapper.text()).toContain('嵌入模型')
    expect(wrapper.findAll('.model-readiness-issues li').map(item => item.text())).toEqual([
      '缺少语言模型 API Key', '缺少语言模型名称', '本地模型文件不存在',
    ])
    await button(wrapper, '＋ 新建书目').trigger('click')
    expect(mocks.readiness).toHaveBeenCalledTimes(1)
  })

  it.each(['request failure', 'incomplete response'])('keeps creation blocked on %s and allows a later retry', async failure => {
    if (failure === 'request failure') mocks.readiness.mockRejectedValue(new Error('network unavailable'))
    else mocks.readiness.mockResolvedValue({ ready: true })
    const wrapper = await home(false)
    await button(wrapper, '创建第一本书').trigger('click'); await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
    expect(mocks.message.error).toHaveBeenCalled()
    expect(mocks.create).not.toHaveBeenCalled()
    mocks.readiness.mockResolvedValue(readyModels)
    await button(wrapper, '＋ 新建书目').trigger('click'); await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(true)
  })

  it('rechecks before submission, blocks newly missing embeddings, and preserves the filled draft', async () => {
    const wrapper = await home()
    await nextStep(wrapper)
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.xuanhuan"]').trigger('click')
    await wrapper.get('[data-testid="market-theme-theme.xuanhuan.dongfang"]').trigger('click')
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await nextStep(wrapper)
    mocks.readiness.mockResolvedValue({ ...readyModels, ready: false, embedding: { ready: false, reasons: ['未配置嵌入模型'], mode: 'openai' } })
    await createButton(wrapper).trigger('click'); await flushPromises()
    expect(mocks.readiness).toHaveBeenCalledTimes(2)
    expect(mocks.create).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请先配置好模型')
    expect(localStorage.getItem('taleweft-new-book-draft')).toContain('测试梗概')
    await button(wrapper, '取消').trigger('click')
    expect(wrapper.find('.create-book-modal').exists()).toBe(true)
    expect((wrapper.get('textarea.premise-input').element as HTMLTextAreaElement).value).toBe('测试梗概：主角寻找改变生活的机会。')
    await createButton(wrapper).trigger('click'); await flushPromises()
    await button(wrapper, '打开模型控制台').trigger('click')
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
    expect(wrapper.get('[data-testid="model-panel"]').text()).toContain('embedding')
    expect(localStorage.getItem('taleweft-new-book-draft')).toContain('测试梗概')
    mocks.readiness.mockResolvedValue(readyModels)
    await button(wrapper, '＋ 新建书目').trigger('click'); await flushPromises()
    await nextStep(wrapper); await nextStep(wrapper)
    let resolve!: (value: unknown) => void
    mocks.create.mockReturnValue(new Promise(yes => { resolve = yes }))
    await createButton(wrapper).trigger('click'); await flushPromises()
    await createButton(wrapper).trigger('click'); await flushPromises()
    expect(mocks.create).toHaveBeenCalledTimes(1)
    resolve({ id: 'created', target_chapters: 100 }); await flushPromises()
    expect(wrapper.find('.create-book-modal').exists()).toBe(false)
  })
})
