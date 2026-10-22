import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import EngineMatrixSection from '@/components/settings/sections/EngineMatrixSection.vue'
import { llmControlApi, type LLMControlPanelData, type LLMProfile } from '@/api/llmControl'

const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('naive-ui', async importOriginal => ({ ...await importOriginal<object>(), useMessage: () => messages }))
vi.mock('@/api/llmControl', async importOriginal => ({ ...await importOriginal<object>(), llmControlApi: { getPanel: vi.fn(), saveConfig: vi.fn() } }))
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const Button = defineComponent({ props: ['loading'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.loading, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('input', { value: props.value ?? '', onInput: (event: Event) => emit('update:value', (event.target as HTMLInputElement).value) })
} })
const Select = defineComponent({ props: ['value', 'options'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('select', { value: props.value, onChange: (event: Event) => emit('update:value', (event.target as HTMLSelectElement).value) },
    props.options.map((option: { label: string; value: string }) => h('option', { value: option.value }, option.label)))
} })
function profile(id: string, name: string, endpoint_kind: LLMProfile['endpoint_kind']): LLMProfile {
  return { id, name, preset_key: 'custom-openai-compatible', protocol: 'openai', base_url: 'https://gateway.example/v1', api_key: 'test-key',
    model: 'model', temperature: 0.7, max_tokens: 2048, timeout_seconds: 60, extra_headers: {}, extra_query: {}, extra_body: {}, notes: '',
    use_legacy_chat_completions: false, endpoint_kind, reasoning_effort: 'high', auth_mode: 'none' }
}
let wrapper: VueWrapper
beforeEach(() => vi.clearAllMocks())
afterEach(() => wrapper?.unmount())
async function mounted(profiles: LLMProfile[], independent = false) {
  const data: LLMControlPanelData = { config: { version: 1, active_profile_id: profiles[0]?.id ?? null,
    endpoint_mode: independent ? 'independent' : 'unified', profiles }, presets: [], runtime: {
    source: 'profile', active_profile_id: null, active_profile_name: null, protocol: 'openai', model: 'model', base_url: '', using_mock: false, reason: null,
  } }
  vi.mocked(llmControlApi.getPanel).mockResolvedValue(data)
  vi.mocked(llmControlApi.saveConfig).mockResolvedValue(data)
  wrapper = mount(EngineMatrixSection, { global: { stubs: {
    NGrid: Box, NGi: Box, NInput: Input, NSelect: Select, NButton: Button, NSwitch: true, InferenceCollapse: true, GlobalLLMEntryButton: true,
  } } })
  await flushPromises()
}
async function save() {
  await wrapper.findAll('button').find(button => button.text() === '保存配置')!.trigger('click')
  await flushPromises()
  return vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0]
}

describe('Engine matrix C / R protocol routing', () => {
  it('loads and saves all independent role endpoint choices without conflating C and R', async () => {
    await mounted([profile('main', '主力模型', 'auto'), profile('cheap', '经济模型', 'chat'), profile('kg', '知识图谱模型', 'responses')], true)
    const selects = wrapper.findAll('select')
    expect(selects.map(select => select.element.value)).toEqual(['openai.auto', 'openai.chat', 'openai.responses'])
    await selects[0]!.setValue('openai.responses')
    await selects[1]!.setValue('openai.responses')
    await selects[2]!.setValue('openai.chat')
    const saved = await save()
    expect(saved.endpoint_mode).toBe('independent')
    expect(saved.profiles.map(profile => [profile.protocol, profile.endpoint_kind])).toEqual([
      ['openai', 'responses'], ['openai', 'responses'], ['openai', 'chat'],
    ])
    expect(saved.profiles.every(profile => !profile.use_legacy_chat_completions)).toBe(true)
  })

  it('preserves old automatic routing until the user chooses an explicit protocol', async () => {
    await mounted([profile('main', '主力模型', 'auto')])
    expect((await save()).profiles[0]?.endpoint_kind).toBe('auto')
  })

  it('clears incompatible OpenAI options when switching to Anthropic', async () => {
    await mounted([{ ...profile('main', '主力模型', 'responses'), extra_body: { vendor_openai: true } }])
    await wrapper.findAll('select')[0]!.setValue('anthropic')
    expect((await save()).profiles[0]).toMatchObject({ protocol: 'anthropic', endpoint_kind: 'auto',
      reasoning_effort: null, thinking_budget_tokens: null, auth_mode: 'api_key', extra_body: {}, preset_key: 'claude-official' })
  })

  it('loads, changes and clears thinking settings independently without dropping profile limits', async () => {
    await mounted([
      { ...profile('main', '主力模型', 'responses'), context_window_limit: 64000 },
      { ...profile('cheap', '经济模型', 'chat'), reasoning_effort: 'low' },
      { ...profile('kg', '知识图谱模型', 'auto'), protocol: 'anthropic', reasoning_effort: null, thinking_budget_tokens: 2048 },
    ], true)
    const fields = wrapper.findAllComponents({ name: 'InferenceCollapse' })
    expect(fields).toHaveLength(3)
    expect(fields[0]!.attributes('reasoningeffort')).toBe('high')
    fields[0]!.vm.$emit('update:reasoningEffort', 'medium')
    fields[1]!.vm.$emit('update:reasoningEffort', null)
    fields[2]!.vm.$emit('update:thinkingBudget', 4096)
    await flushPromises()
    const saved = await save()
    expect(saved.profiles[0]).toMatchObject({ reasoning_effort: 'medium', context_window_limit: 64000 })
    expect(saved.profiles[1]?.reasoning_effort).toBeNull()
    expect(saved.profiles[2]).toMatchObject({ protocol: 'anthropic', reasoning_effort: null, thinking_budget_tokens: 4096 })
  })
})
