import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { NSelect } from 'naive-ui'
import LLMControlPanel from '../src/components/workbench/LLMControlPanel.vue'
import { llmControlApi, type LLMControlPanelData, type LLMProfile, type LLMTestResult, type ModelCapabilities, type ModelListResponse } from '../src/api/llmControl'

const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }))
vi.mock('naive-ui', async importOriginal => ({ ...await importOriginal<typeof import('naive-ui')>(), useMessage: () => messages }))
vi.mock('../src/api/llmControl', async importOriginal => ({ ...await importOriginal<typeof import('../src/api/llmControl')>(), llmControlApi: {
  getPanel: vi.fn(), testProfile: vi.fn(), capabilities: vi.fn(), fetchModels: vi.fn(), saveConfig: vi.fn(),
} }))

const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', [slots.header?.(), slots.default?.()]) } })
const Button = defineComponent({ props: ['disabled', 'loading'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled || props.loading, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('input', { value: props.value ?? '', onInput: (event: Event) => emit('update:value', (event.target as HTMLInputElement).value) })
} })
const NumberInput = defineComponent({ props: ['value'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('input', { value: props.value ?? '', onInput: (event: Event) => {
    const value = (event.target as HTMLInputElement).value
    emit('update:value', value === '' ? null : Number(value))
  } })
} })
const Select = defineComponent({ props: ['value', 'options', 'disabled'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('select', { value: props.value ?? '', disabled: props.disabled, onChange: (event: Event) => emit('update:value', (event.target as HTMLSelectElement).value || null) },
    [h('option', { value: '' }, '默认'), ...(props.options ?? []).map((option: { label: string; value: string; disabled?: boolean }) => h('option', { value: option.value, disabled: option.disabled }, option.label))])
} })
// happy-dom has no layout measurements for virtual lists; exercise the real
// select and its filtering/selection with only virtualization disabled.
const DomSelect = { ...NSelect, props: { ...NSelect.props, virtualScroll: { type: Boolean, default: false } } }

const testResult: LLMTestResult = { ok: true, provider_label: 'A', model: 'model-a', latency_ms: 10, preview: '', error: null,
  requested_parameters: { marker: 'original-request' }, submitted_parameters: { max_tokens: 1024 },
  provider_confirmation_status: 'not_reported', provider_confirmed_parameters: {} }
const capability: ModelCapabilities = { model: 'model-a', max_output_tokens: 2048, context_window: 8192, source: 'capability-source', valid: true, effective_max_tokens: 1024 }
function profile(id: string, protocol: LLMProfile['protocol'] = 'openai'): LLMProfile {
  return { id, name: `配置 ${id}`, preset_key: 'custom', protocol, base_url: `https://${id}.example/v1`, api_key: `key-${id}`, model: `model-${id}`,
    temperature: 0.7, max_tokens: 1024, timeout_seconds: 60, extra_headers: {}, extra_query: {}, extra_body: {}, notes: '',
    use_legacy_chat_completions: false, endpoint_kind: 'auto', auth_mode: 'api_key', reasoning_effort: protocol === 'openai' ? 'high' : null,
    thinking_budget_tokens: protocol === 'openai' ? null : 1024, max_output_tokens_limit: 4096, context_window_limit: 16384 }
}
function panel(profiles = [profile('a'), profile('b', 'anthropic')]): LLMControlPanelData {
  return { config: { version: 1, active_profile_id: 'a', profiles }, presets: [], runtime: {
    source: 'profile', active_profile_id: 'a', active_profile_name: '配置 a', protocol: 'openai', model: 'model-a', base_url: '', using_mock: false, reason: null,
  } }
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
let wrapper: VueWrapper
async function mounted(data = panel(), realSelect = false) {
  vi.mocked(llmControlApi.getPanel).mockResolvedValue(data)
  wrapper = mount(LLMControlPanel, { attachTo: document.body, global: { components: { NSelect: DomSelect }, stubs: {
    NButton: Button, NInput: Input, NAutoComplete: Input, NInputNumber: NumberInput, NSelect: realSelect ? false : Select,
    NCard: Box, NSpace: Box, NTag: Box, NText: Box, NAlert: Box, NCollapse: Box, NCollapseItem: Box, NEmpty: Box, BookModelUsage: true,
  } } })
  await flushPromises()
  return wrapper
}
function button(label: string) { return wrapper.findAll('button').find(item => item.text() === label)! }
function field(label: string) { return wrapper.findAll('.llm-field').find(item => item.find('.llm-label').exists() && item.find('.llm-label').text() === label)! }
async function input(label: string, value: string) { await field(label).get('input').setValue(value) }
async function select(label: string, value: string) { await field(label).get('select').setValue(value) }
async function establishResults() {
  await button('查看有效能力').trigger('click'); await flushPromises()
  await button('测试连接').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('capability-source')
  expect(wrapper.text()).toContain('original-request')
}
function expectResultsCleared() {
  expect(wrapper.text()).not.toContain('capability-source')
  expect(wrapper.text()).not.toContain('original-request')
}
beforeEach(() => {
  vi.mocked(llmControlApi.testProfile).mockResolvedValue(testResult)
  vi.mocked(llmControlApi.capabilities).mockResolvedValue(capability)
  vi.mocked(llmControlApi.fetchModels).mockResolvedValue({ success: true, items: [{ id: 'remote-model-a', name: '', owned_by: '' }], count: 1 })
  vi.mocked(llmControlApi.saveConfig).mockImplementation(async config => panel(config.profiles))
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = ''; vi.clearAllMocks() })

describe('LLM control draft and request ownership', () => {
  it.each([
    ['openai', '模型思考程度'], ['anthropic', '模型思考预算（token）'],
  ] as const)('presents %s thinking controls immediately after model selection', async (protocol, label) => {
    await mounted(panel([profile('a', protocol)]))
    expect(field('模型名').element.nextElementSibling?.querySelector('.llm-label')?.textContent).toBe(label)
    expect(field(label).classes()).toContain('span-2')
  })

  it.each([
    { endpoint_kind: 'chat', use_legacy_chat_completions: false },
    { endpoint_kind: 'responses', use_legacy_chat_completions: false },
    { endpoint_kind: undefined, use_legacy_chat_completions: true },
  ] as const)('migrates the old endpoint choice $endpoint_kind to automatic routing for test and save', async legacy => {
    await mounted(panel([{ ...profile('a'), ...legacy }]))
    expect(field('协议').get('select').element.value).toBe('openai.auto')
    expect(field('协议').findAll('option').map(option => option.element.value)).toEqual(['', 'openai.auto', 'anthropic', 'gemini'])
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0]).toMatchObject({
      protocol: 'openai', endpoint_kind: 'auto', use_legacy_chat_completions: false,
    })
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]).toMatchObject({
      protocol: 'openai', endpoint_kind: 'auto', use_legacy_chat_completions: false,
    })
    expect(field('协议').get('select').element.value).toBe('openai.auto')
  })

  it('keeps an existing auto choice on save and creates an empty automatic profile', async () => {
    await mounted(); await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]?.endpoint_kind).toBe('auto')
    await button('新增').trigger('click')
    expect(field('协议').get('select').element.value).toBe('openai.auto')
    expect(field('Base URL').get('input').element.value).toBe('')
    expect(field('模型名').get('input').element.value).toBe('')
  })

  it('has no preset import or selector even when the server returns vendor presets', async () => {
    const data = panel()
    data.presets = [{ key: 'responses', label: 'OpenAI R', protocol: 'openai', endpoint_kind: 'responses',
      default_base_url: '', default_model: '', description: '', tags: [] }]
    await mounted(data)
    expect(wrapper.text()).not.toContain('导入厂商预设')
    expect(wrapper.text()).not.toContain('应用预设')
    expect(wrapper.find('.llm-preset-card').exists()).toBe(false)
    expect(wrapper.findAll('.llm-label').map(label => label.text())).not.toContain('预设')
  })

  it.each([
    ['模型名', 'changed-model', 'input'], ['Base URL', 'https://changed.example/v1', 'input'], ['API Key', 'changed-key', 'input'],
    ['协议', 'anthropic', 'select'], ['认证方式', 'none', 'select'], ['模型思考程度', 'low', 'select'],
    ['默认 max_tokens', '1536', 'input'], ['默认 temperature', '0.9', 'input'], ['超时（秒）', '120', 'input'],
    ['默认 top_p', '0.85', 'input'], ['停止序列（JSON 字符串数组）', '["END"]', 'input'],
    ['模型输出上限（未知可留空）', '6000', 'input'], ['模型上下文上限（未知可留空）', '32768', 'input'],
    ['extra_headers（JSON）', '{"x-tenant":"new"}', 'input'], ['extra_query（JSON）', '{"api-version":"new"}', 'input'],
    ['extra_body（JSON）', '{"vendor_option":"new"}', 'input'],
  ])('clears stale capability and test receipt when %s changes', async (label, value, kind) => {
    await mounted(); await establishResults()
    if (kind === 'select') await select(label, value)
    else await input(label, value)
    expectResultsCleared()
  })

  it('clears results immediately when a thinking budget changes', async () => {
    await mounted(panel([profile('a', 'anthropic')])); await establishResults()
    await input('模型思考预算（token）', '2048')
    expectResultsCleared()
  })

  it('keeps both profiles’ settings and advanced drafts while switching', async () => {
    await mounted()
    await input('extra_body（JSON）', '{"vendor_option":"a-value"}')
    await wrapper.get('.llm-profile-list button:nth-child(2)').trigger('click')
    expect(field('模型思考预算（token）').get('input').element.value).toBe('1024')
    await input('extra_body（JSON）', '{"vendor_option":"b-value"}')
    await wrapper.get('.llm-profile-list button:nth-child(1)').trigger('click')
    expect(field('模型思考程度').get('select').element.value).toBe('high')
    expect(field('extra_body（JSON）').get('input').element.value).toContain('a-value')
    await button('保存配置').trigger('click'); await flushPromises()
    const saved = vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles
    expect(saved.find(item => item.id === 'b')?.thinking_budget_tokens).toBe(1024)
    expect(saved.find(item => item.id === 'b')?.extra_body).toEqual({ vendor_option: 'b-value' })
  })

  it('clears incompatible protocol options only when the same profile changes protocol', async () => {
    const first = { ...profile('a'), endpoint_kind: 'chat' as const, auth_mode: 'none' as const, use_legacy_chat_completions: true, extra_body: { vendor_option: 'old-openai' } }
    await mounted(panel([first])); await establishResults()
    await select('协议', 'anthropic'); expectResultsCleared()
    expect(field('extra_body（JSON）').get('input').element.value).toBe('{}')
    await input('模型思考预算（token）', '2048')
    await select('协议', 'openai.auto')
    await button('测试连接').trigger('click'); await flushPromises()
    const sent = vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0]
    expect(sent.reasoning_effort).toBeNull()
    expect(sent.thinking_budget_tokens).toBeNull()
    expect(sent.endpoint_kind).toBe('auto')
    expect(sent.auth_mode).toBe('api_key')
    expect(sent.use_legacy_chat_completions).toBe(false)
    expect(sent.extra_body).toEqual({})
  })

  it('preserves loaded protocol options when refreshing the same saved profile', async () => {
    await mounted()
    vi.mocked(llmControlApi.getPanel).mockResolvedValue(panel([{ ...profile('a', 'anthropic'), extra_body: { vendor_option: 'saved-value' } }]))
    await button('刷新').trigger('click'); await flushPromises()
    expect(field('模型思考预算（token）').get('input').element.value).toBe('1024')
    expect(field('extra_body（JSON）').get('input').element.value).toContain('saved-value')
  })

  it('preserves model and advanced drafts entered while a refresh is pending', async () => {
    await mounted()
    const refreshing = deferred<LLMControlPanelData>()
    vi.mocked(llmControlApi.getPanel).mockReturnValue(refreshing.promise)
    await button('刷新').trigger('click')
    await input('模型名', 'typed-during-refresh')
    await input('extra_body（JSON）', '{"draft":"keep-during-refresh"}')
    refreshing.resolve(panel()); await flushPromises()
    expect(field('模型名').get('input').element.value).toBe('typed-during-refresh')
    expect(field('extra_body（JSON）').get('input').element.value).toBe('{"draft":"keep-during-refresh"}')
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]).toMatchObject({
      model: 'typed-during-refresh', extra_body: { draft: 'keep-during-refresh' },
    })
  })

  it('keeps the newest refresh when an older refresh completes last', async () => {
    await mounted()
    const older = deferred<LLMControlPanelData>(), newer = deferred<LLMControlPanelData>()
    vi.mocked(llmControlApi.getPanel).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const setup = (wrapper.vm as any).$.setupState
    void setup.loadPanel()
    void setup.loadPanel()
    newer.resolve(panel([{ ...profile('a'), model: 'newest-model' }])); await flushPromises()
    expect(field('模型名').get('input').element.value).toBe('newest-model')
    older.resolve(panel([{ ...profile('a'), model: 'obsolete-model' }])); await flushPromises()
    expect(field('模型名').get('input').element.value).toBe('newest-model')
    expect(wrapper.emitted('panel-updated')?.at(-1)?.[0]).toMatchObject({ config: { profiles: [{ model: 'newest-model' }] } })
  })

  it('ignores late test, capability and model-list responses after changing profiles', async () => {
    const test = deferred<LLMTestResult>(), caps = deferred<ModelCapabilities>(), models = deferred<ModelListResponse>()
    vi.mocked(llmControlApi.testProfile).mockReturnValue(test.promise)
    vi.mocked(llmControlApi.capabilities).mockReturnValue(caps.promise)
    vi.mocked(llmControlApi.fetchModels).mockReturnValue(models.promise)
    await mounted()
    await button('测试连接').trigger('click'); await button('读取上游模型能力').trigger('click'); await button('拉取模型').trigger('click')
    await wrapper.get('.llm-profile-list button:nth-child(2)').trigger('click')
    test.resolve(testResult); caps.resolve({ ...capability, source: 'upstream_model_metadata' }); models.resolve({ success: true, items: [{ id: 'remote-model-a', name: '', owned_by: '' }], count: 1 })
    await flushPromises()
    expectResultsCleared()
    expect(wrapper.text()).not.toContain('upstream_model_metadata')
    expect(wrapper.text()).not.toContain('已拉取 1 个模型')
    expect(field('模型输出上限（未知可留空）').get('input').element.value).toBe('4096')
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('ignores late same-profile results and sends an immutable request snapshot', async () => {
    const test = deferred<LLMTestResult>(), caps = deferred<ModelCapabilities>()
    vi.mocked(llmControlApi.testProfile).mockReturnValue(test.promise)
    vi.mocked(llmControlApi.capabilities).mockReturnValue(caps.promise)
    await mounted(); await button('测试连接').trigger('click'); await button('读取上游模型能力').trigger('click')
    await input('模型名', 'model-changed')
    expect(vi.mocked(llmControlApi.testProfile).mock.calls[0]![0].model).toBe('model-a')
    expect(vi.mocked(llmControlApi.capabilities).mock.calls[0]![0].model).toBe('model-a')
    test.resolve(testResult); caps.resolve({ ...capability, source: 'upstream_model_metadata' }); await flushPromises()
    expectResultsCleared()
    expect(field('模型输出上限（未知可留空）').get('input').element.value).toBe('4096')
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('keeps the newest test in flight when an obsolete request finishes first', async () => {
    const old = deferred<LLMTestResult>(), current = deferred<LLMTestResult>()
    vi.mocked(llmControlApi.testProfile).mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    await mounted(); await button('测试连接').trigger('click')
    await input('模型名', 'model-new'); await button('测试连接').trigger('click')
    old.resolve(testResult); await flushPromises()
    expectResultsCleared()
    expect(button('测试连接').element.disabled).toBe(true)
    current.resolve({ ...testResult, requested_parameters: { marker: 'new-request' } }); await flushPromises()
    expect(wrapper.text()).toContain('new-request')
    expect(wrapper.text()).not.toContain('original-request')
    expect(button('测试连接').element.disabled).toBe(false)
  })

  it('applies current upstream limits and requires a new test for the changed configuration', async () => {
    await mounted(); await establishResults()
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability, source: 'upstream_model_metadata' })
    await button('读取上游模型能力').trigger('click'); await flushPromises()
    expect(field('模型输出上限（未知可留空）').get('input').element.value).toBe('2048')
    expect(wrapper.text()).toContain('upstream_model_metadata')
    expect(wrapper.text()).not.toContain('original-request')
    expect(button('读取上游模型能力').element.disabled).toBe(false)
  })

  it('suppresses errors from expired requests and rejects malformed advanced JSON before a probe', async () => {
    const old = deferred<LLMTestResult>()
    vi.mocked(llmControlApi.testProfile).mockReturnValue(old.promise)
    await mounted(); await button('测试连接').trigger('click'); await input('模型名', 'changed-model')
    old.reject(new Error('old profile failed')); await flushPromises()
    expect(messages.error).not.toHaveBeenCalled()
    const capabilityCallsBeforeInvalidJson = vi.mocked(llmControlApi.capabilities).mock.calls.length
    await input('extra_body（JSON）', '{'); await button('查看有效能力').trigger('click'); await flushPromises()
    expect(llmControlApi.capabilities).toHaveBeenCalledTimes(capabilityCallsBeforeInvalidJson)
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('extra_body 格式错误'))
  })

  it('filters supported reasoning levels without losing the capability when the effort changes', async () => {
    await mounted()
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability,
      features: { reasoning_effort: 'configurable' }, reasoning_efforts: ['low', 'high'] })
    await button('查看有效能力').trigger('click'); await flushPromises()
    const effort = field('模型思考程度').get('select')
    expect(effort.findAll('option').map(option => option.element.value)).toEqual(['', 'low', 'high'])
    await effort.setValue('low')
    expect(field('模型思考程度').get('select').element.value).toBe('low')
    expect(field('模型思考程度').findAll('option').map(option => option.element.value)).toEqual(['', 'low', 'high'])
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]?.reasoning_effort).toBe('low')
  })

  it('keeps an unsupported saved effort visible, blocks save/test, and allows clearing it', async () => {
    await mounted()
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability,
      features: { reasoning_effort: 'configurable' }, reasoning_efforts: ['low', 'medium'] })
    await button('查看有效能力').trigger('click'); await flushPromises()
    const effort = field('模型思考程度').get('select')
    expect(effort.element.value).toBe('high')
    expect(effort.findAll('option').map(option => option.element.value)).toEqual(['', 'low', 'medium', 'high'])
    expect(effort.findAll('option').at(-1)?.element.disabled).toBe(true)
    expect(field('模型思考程度').text()).toContain('已保留原草稿值')
    await button('保存配置').trigger('click'); await button('测试连接').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).not.toHaveBeenCalled()
    expect(llmControlApi.testProfile).not.toHaveBeenCalled()
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('当前模型不支持“high”'))
    await effort.setValue('')
    await button('保存配置').trigger('click'); await button('测试连接').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).toHaveBeenCalledOnce()
    expect(llmControlApi.testProfile).toHaveBeenCalledOnce()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].reasoning_effort).toBeNull()
  })

  it('treats none as an explicit effort that a reviewed model must allow', async () => {
    await mounted(panel([{ ...profile('a'), reasoning_effort: 'none' }]))
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability,
      features: { reasoning_effort: 'configurable' }, reasoning_efforts: ['low'] })
    await button('查看有效能力').trigger('click'); await flushPromises()
    const effort = field('模型思考程度').get('select')
    expect(effort.findAll('option').map(option => option.element.value)).toEqual(['', 'low', 'none'])
    expect(effort.findAll('option').at(-1)?.element.disabled).toBe(true)
    expect(field('模型思考程度').text()).toContain('已保留原草稿值')
    await button('保存配置').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).not.toHaveBeenCalled()
    await effort.setValue('low')
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]?.reasoning_effort).toBe('low')
  })

  it('blocks a known unavailable model until the effort is cleared and keeps unknown gateway choices', async () => {
    await mounted()
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability,
      features: { reasoning_effort: 'unavailable' }, reasoning_efforts: [] })
    await button('查看有效能力').trigger('click'); await flushPromises()
    let effort = field('模型思考程度').get('select')
    expect(effort.findAll('option').map(option => option.element.value)).toEqual(['', 'high'])
    expect(effort.findAll('option').at(-1)?.element.disabled).toBe(true)
    expect(field('模型思考程度').text()).toContain('不支持思考程度')
    await button('保存配置').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).not.toHaveBeenCalled()
    await effort.setValue('')
    expect(effort.element.value).toBe('')
    expect(field('模型思考程度').get('select').element.disabled).toBe(true)
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]?.reasoning_effort).toBeNull()

    await input('模型名', 'private-gateway-model')
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability, model: 'private-gateway-model',
      features: { reasoning_effort: 'model_dependent' }, reasoning_efforts: [] })
    await button('查看有效能力').trigger('click'); await flushPromises()
    effort = field('模型思考程度').get('select')
    expect(effort.findAll('option').map(option => option.element.value)).toEqual(['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
    expect(field('模型思考程度').text()).toContain('尚未确认')
    await effort.setValue('none')
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].reasoning_effort).toBe('none')
    await effort.setValue('max')
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].reasoning_effort).toBe('max')
  })

  it('queries a reasoning-free copy without changing unsupported effort or nested advanced drafts', async () => {
    const advanced = { reasoning: { effort: 'max' }, reasoning_effort: 'max',
      generationConfig: { reasoning: { effort: 'max' }, reasoningConfig: { effort: 'max' }, other: 'preserved' },
      vendor_option: 'preserved' }
    const first = { ...profile('a'), reasoning_effort: 'max' as const, extra_body: advanced }
    await mounted(panel([first]))
    const queried = vi.mocked(llmControlApi.capabilities).mock.calls[0]![0]
    expect(queried.reasoning_effort).toBeNull()
    expect(queried.extra_body).toEqual({ generationConfig: { other: 'preserved' }, vendor_option: 'preserved' })
    expect(field('模型思考程度').get('select').element.value).toBe('max')
    expect(JSON.parse(field('extra_body（JSON）').get('input').element.value)).toEqual(advanced)
  })

  it('automatically refreshes capabilities when model identity changes and ignores a late prior identity', async () => {
    const old = deferred<ModelCapabilities>()
    vi.mocked(llmControlApi.capabilities).mockReset()
    vi.mocked(llmControlApi.capabilities).mockReturnValueOnce(old.promise).mockResolvedValueOnce({ ...capability,
      model: 'model-new', features: { reasoning_effort: 'configurable' }, reasoning_efforts: ['low'] })
    await mounted(); await flushPromises()
    expect(vi.mocked(llmControlApi.capabilities)).toHaveBeenCalledOnce()
    expect(vi.mocked(llmControlApi.capabilities).mock.calls[0]![0]).toMatchObject({ model: 'model-a', reasoning_effort: null })
    await input('模型名', 'model-new'); await flushPromises()
    expect(vi.mocked(llmControlApi.capabilities)).toHaveBeenCalledTimes(2)
    expect(vi.mocked(llmControlApi.capabilities).mock.calls[1]![0]).toMatchObject({ model: 'model-new', reasoning_effort: null })
    old.resolve({ ...capability, model: 'model-old', features: { reasoning_effort: 'unavailable' }, reasoning_efforts: [] })
    await flushPromises()
    const values = field('模型思考程度').findAll('option').map(option => option.element.value)
    expect(values).toEqual(['', 'low', 'high'])
    expect(field('模型思考程度').findAll('option').at(-1)?.element.disabled).toBe(true)
  })

  it('submits top_p and exact stop sequences and shows declared protocol support', async () => {
    await mounted()
    await input('默认 top_p', '0.85')
    await input('停止序列（JSON 字符串数组）', '[" END ", "\\n\\n"]')
    vi.mocked(llmControlApi.capabilities).mockResolvedValue({ ...capability, features: { top_p: 'configurable', stop_sequences: 'unavailable' } })
    await button('查看有效能力').trigger('click'); await flushPromises()
    expect(field('默认 top_p').text()).toContain('支持此参数')
    expect(field('停止序列（JSON 字符串数组）').text()).toContain('不支持此参数')
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0]).toMatchObject({ top_p: 0.85, stop_sequences: [' END ', '\n\n'] })
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]).toMatchObject({ top_p: 0.85, stop_sequences: [' END ', '\n\n'] })
  })

  it.each(['{}', '[1]', '[""]', '['])('rejects invalid stop sequence input %s before save, test or probe', async stops => {
    await mounted(); const capabilityCallsBeforeInvalidJson = vi.mocked(llmControlApi.capabilities).mock.calls.length
    await input('停止序列（JSON 字符串数组）', stops)
    await button('保存配置').trigger('click'); await button('测试连接').trigger('click'); await button('查看有效能力').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).not.toHaveBeenCalled()
    expect(llmControlApi.testProfile).not.toHaveBeenCalled()
    expect(llmControlApi.capabilities).toHaveBeenCalledTimes(capabilityCallsBeforeInvalidJson)
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('停止序列格式错误'))
  })

  it('preserves whitespace stops and the maximum stop length exactly', async () => {
    const stops = [' ', '\n', '终'.repeat(1024)]
    await mounted(); await input('停止序列（JSON 字符串数组）', JSON.stringify(stops))
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].stop_sequences).toEqual(stops)
  })

  it.each([{ stops: Array.from({ length: 17 }, () => 'end') }, { stops: ['终'.repeat(1025)] }])('rejects stops beyond the shared contract', async ({ stops }) => {
    await mounted(); const capabilityCallsBeforeInvalidJson = vi.mocked(llmControlApi.capabilities).mock.calls.length
    await input('停止序列（JSON 字符串数组）', JSON.stringify(stops))
    await button('保存配置').trigger('click'); await button('测试连接').trigger('click'); await button('查看有效能力').trigger('click'); await flushPromises()
    expect(llmControlApi.saveConfig).not.toHaveBeenCalled()
    expect(llmControlApi.testProfile).not.toHaveBeenCalled()
    expect(llmControlApi.capabilities).toHaveBeenCalledTimes(capabilityCallsBeforeInvalidJson)
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('最多 16 项，每项最多 1024 个字符'))
  })

  it('keeps fetched model options while editing the model name and revokes them when its gateway changes', async () => {
    await mounted(); await button('拉取模型').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('已拉取 1 个模型')
    await select('模型名', 'remote-model-a')
    expect(wrapper.text()).toContain('已拉取 1 个模型')
    await input('Base URL', 'https://other.example/v1')
    expect(wrapper.text()).not.toContain('已拉取 1 个模型')
  })

  it('opens every fetched model immediately and searches without replacing the current model', async () => {
    vi.mocked(llmControlApi.fetchModels).mockResolvedValue({ success: true, count: 2, items: [
      { id: 'remote-model-a', name: '', owned_by: '' }, { id: 'remote-model-b', name: '', owned_by: '' },
    ] })
    await mounted(panel(), true)
    await button('拉取模型').trigger('click'); await flushPromises()
    const options = () => Array.from(document.querySelectorAll<HTMLElement>('.n-base-select-option'))
    expect(options().map(option => option.textContent)).toEqual(['remote-model-a', 'remote-model-b'])
    await field('模型名').get('input').setValue('remote-model-b'); await flushPromises()
    expect(options().map(option => option.textContent)).toEqual(['remote-model-b'])
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].model).toBe('model-a')
    options().find(option => option.textContent === 'remote-model-b')!.click(); await flushPromises()
    await button('保存配置').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.saveConfig).mock.calls.at(-1)![0].profiles[0]?.model).toBe('remote-model-b')
  })

  it.each(['empty', 'failure'])('keeps a manual model entry when model fetching returns %s', async outcome => {
    if (outcome === 'empty') vi.mocked(llmControlApi.fetchModels).mockResolvedValue({ success: true, count: 0, items: [] })
    else vi.mocked(llmControlApi.fetchModels).mockRejectedValue(new Error('upstream unavailable'))
    await mounted(); await button('拉取模型').trigger('click'); await flushPromises()
    await input('模型名', 'custom-gateway-model')
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0].model).toBe('custom-gateway-model')
  })

  it('fetches models with validated current header and query drafts and rejects stale authentication results', async () => {
    const old = deferred<ModelListResponse>()
    vi.mocked(llmControlApi.fetchModels).mockReturnValue(old.promise)
    await mounted(); await input('extra_headers（JSON）', '{"x-tenant":"new"}'); await input('extra_query（JSON）', '{"api-version":"new"}')
    await button('拉取模型').trigger('click')
    expect(vi.mocked(llmControlApi.fetchModels).mock.calls.at(-1)![0]).toMatchObject({ extra_headers: { 'x-tenant': 'new' }, extra_query: { 'api-version': 'new' } })
    await input('extra_headers（JSON）', '{"x-tenant":"other"}')
    old.resolve({ success: true, count: 1, items: [{ id: 'stale-model', name: 'stale-model' }] }); await flushPromises()
    expect(wrapper.text()).not.toContain('已拉取 1 个模型')
    await input('extra_query（JSON）', '{'); await button('拉取模型').trigger('click'); await flushPromises()
    expect(llmControlApi.fetchModels).toHaveBeenCalledTimes(1)
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('extra_query 格式错误'))
  })

  it('preserves explicit zero top_p and an empty stop list', async () => {
    await mounted()
    await input('默认 top_p', '0'); await input('停止序列（JSON 字符串数组）', '[]')
    await button('测试连接').trigger('click'); await flushPromises()
    expect(vi.mocked(llmControlApi.testProfile).mock.calls.at(-1)![0]).toMatchObject({ top_p: 0, stop_sequences: [] })
    expect(wrapper.text()).toContain('已启用思考并显式设置 top_p')
  })

  it('shows unavailable model configuration and the reason without promising simulated generation', async () => {
    const initial = panel()
    initial.runtime = { ...initial.runtime, source: 'unavailable', status: 'unavailable', using_mock: false, reason: '当前配置缺少模型名称' }
    await mounted(initial)
    expect(wrapper.text()).toContain('运行中：模型配置不可用')
    expect(wrapper.text()).toContain('当前配置缺少模型名称')
    expect(wrapper.text()).not.toContain('MockProvider')
  })

  it('preserves inputs entered while a save is pending and keeps independent endpoint mode', async () => {
    const saving = deferred<LLMControlPanelData>()
    vi.mocked(llmControlApi.saveConfig).mockReturnValue(saving.promise)
    const initial = panel(); initial.config.endpoint_mode = 'independent'
    await mounted(initial); await button('保存配置').trigger('click')
    await input('模型名', 'typed-after-save')
    await input('extra_body（JSON）', '{"draft":"keep-me"}')
    const sent = vi.mocked(llmControlApi.saveConfig).mock.calls[0]![0]
    expect(sent.endpoint_mode).toBe('independent')
    expect(sent.profiles[0]!.model).toBe('model-a')
    saving.resolve(panel(sent.profiles)); await flushPromises()
    expect(field('模型名').get('input').element.value).toBe('typed-after-save')
    expect(field('extra_body（JSON）').get('input').element.value).toBe('{"draft":"keep-me"}')
    expect(messages.success).toHaveBeenCalledWith(expect.stringContaining('新修改已保留'))
  })

  it('preserves the draft and current verified receipts when saving fails', async () => {
    await mounted(); await input('extra_body（JSON）', '{"draft":"persist"}'); await establishResults()
    vi.mocked(llmControlApi.saveConfig).mockRejectedValue(new Error('storage unavailable'))
    await button('保存配置').trigger('click'); await flushPromises()
    expect(field('extra_body（JSON）').get('input').element.value).toBe('{"draft":"persist"}')
    expect(wrapper.text()).toContain('original-request')
    expect(wrapper.text()).toContain('capability-source')
    expect(messages.error).toHaveBeenCalledWith(expect.stringContaining('storage unavailable'))
    expect(button('保存配置').element.disabled).toBe(false)
  })

  it('preserves advanced drafts before adding a profile', async () => {
    const data = panel()
    data.presets = [{ key: 'custom', label: '新预设', protocol: 'openai', default_base_url: '', default_model: '', description: '', tags: [] }]
    await mounted(data)
    await input('extra_body（JSON）', '{"draft":"keep-before-switch"}')
    await input('停止序列（JSON 字符串数组）', '["END"]')
    await button('新增').trigger('click')
    await wrapper.get('.llm-profile-list button:first-child').trigger('click')
    expect(field('extra_body（JSON）').get('input').element.value).toContain('keep-before-switch')
    expect(field('停止序列（JSON 字符串数组）').get('input').element.value).toContain('END')
  })
})
