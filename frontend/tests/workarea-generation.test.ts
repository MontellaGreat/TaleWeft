import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createPinia } from 'pinia'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import WorkArea from '../src/components/workbench/WorkArea.vue'

const state = vi.hoisted(() => ({ consume: vi.fn(), cancel: vi.fn(), getChapter: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() } }))
vi.mock('../src/api/workflow', () => ({ consumeGenerateChapterStream: state.consume, cancelChapterGeneration: state.cancel,
  analyzeScene: vi.fn(), retrieveContext: vi.fn(), saveChapterDraft: vi.fn(), parseStreamGeneratedBeats: () => [] }))
vi.mock('../src/api/aiInvocation', () => ({ aiInvocationApi: {} }))
vi.mock('../src/api/tools', () => ({ tensionApi: {} }))
vi.mock('../src/api/chapter', () => ({ chapterApi: { getChapter: state.getChapter, getGuardrailSnapshot: async () => null }, getChapterRevisionConflict: () => null }))
vi.mock('../src/api/llmControl', () => ({ llmControlApi: { getConfig: async () => ({ config: { profiles: [] } }) } }))
vi.mock('../src/workbench/useAssistedAutopilotStatus', () => ({ useAssistedAutopilotStatus: () => undefined }))
vi.mock('naive-ui', () => ({ useMessage: () => state.message, useDialog: () => ({ warning: vi.fn() }) }))
vi.mock('../src/components/autopilot/AutopilotWorkspace.vue', () => ({ default: { name: 'AutopilotWorkspace', render: () => null } }))
vi.mock('../src/components/autopilot/AutopilotWritingStream.vue', () => ({ default: { name: 'AutopilotWritingStream', render: () => null } }))
vi.mock('../src/components/workbench/ChapterContentPanel.vue', () => ({ default: { name: 'ChapterContentPanel', render: () => null } }))
vi.mock('../src/components/workbench/ChapterElementPanel.vue', () => ({ default: { name: 'ChapterElementPanel', render: () => null } }))
vi.mock('../src/components/workbench/ChapterStatusPanel.vue', () => ({ default: { name: 'ChapterStatusPanel', render: () => null } }))

const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', Object.values(slots).map(slot => slot?.())) } })
const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value', 'readonly', 'placeholder'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('textarea', { value: props.value, readonly: props.readonly, placeholder: props.placeholder,
    onInput: (event: any) => { if (!props.readonly) emit('update:value', event.target.value) } })
} })
const stubs: Record<string, any> = { NInput: Input, NButton: Button }
for (const name of ['NSpace', 'NCard', 'NAlert', 'NScrollbar', 'NModal', 'NFormItem', 'NTabs', 'NTabPane', 'NText', 'NCollapse',
  'NCollapseItem', 'NTooltip', 'NSelect', 'NSwitch', 'NDivider', 'NSpin', 'NTag', 'NProgress', 'NCode', 'NButtonGroup', 'NIcon',
  'NSkeleton', 'NInputNumber', 'NRadioGroup', 'NRadio', 'NCheckbox', 'NPopover', 'NEmpty', 'NDynamicInput', 'ChapterWorkbenchShell']) stubs[name] = Box
for (const name of ['ChapterContentPanel', 'ChapterElementPanel', 'ChapterStatusPanel', 'QualityGuardrailPanel', 'TraceRecordPanel',
  'AutopilotWorkspace', 'AutopilotWritingStream', 'ChapterHistoryModal']) stubs[name] = true
let wrappers: VueWrapper[] = []
const deferred = () => { let resolve!: () => void; const promise = new Promise<void>(ok => { resolve = ok }); return { resolve, promise } }
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(candidate => candidate.text() === text)!
async function open() {
  const chapter = { id: 1, number: 1, title: '第一章', word_count: 0, revision: 0, content_hash: 'hash', etag: 'etag' }
  const wrapper = mount(WorkArea, { props: { slug: 'book', chapters: [chapter], currentChapterId: 1, chapterContent: '', chapterVersion: chapter },
    global: { plugins: [createPinia()], stubs } })
  wrappers.push(wrapper)
  await flushPromises()
  const setup = (wrapper.vm as any).$.setupState
  setup.showGenerateModal = true
  setup.generateTargetChapterId = 1
  await flushPromises()
  return { wrapper, setup }
}
beforeEach(() => {
  vi.clearAllMocks()
  state.getChapter.mockResolvedValue({ id: 'chapter', number: 1, content: '', revision: 0, content_hash: 'hash', etag: 'etag' })
  state.cancel.mockResolvedValue({ status: 'cancelled' })
  sessionStorage.clear()
})
afterEach(async () => { wrappers.forEach(wrapper => wrapper.unmount()); wrappers = []; await flushPromises(); sessionStorage.clear() })

describe('actual WorkArea generation state', () => {
  it('unknown cancellation remains visible and retries the same request until confirmed', async () => {
    const running = deferred()
    state.consume.mockImplementation(async (_slug, data, handlers) => {
      handlers.onRequest(data.request_id)
      handlers.onChunk('已有片段')
      await running.promise
    })
    state.cancel.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ status: 'running' }).mockResolvedValue({ status: 'cancelled' })
    const { wrapper, setup } = await open()
    const generate = setup.handleStartGenerate()
    await flushPromises()
    await button(wrapper, '停止').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('停止尚未确认')
    expect(wrapper.find('textarea[placeholder="生成的内容将在此显示..."]').attributes('readonly')).toBeDefined()
    await button(wrapper, '重试停止').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('停止尚未确认')
    expect(state.message.info).not.toHaveBeenCalled()
    await button(wrapper, '重试停止').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已停止，片段已保留')
    expect(new Set(state.cancel.mock.calls.slice(0, 3).map(call => call[1])).size).toBe(1)
    expect(wrapper.find('textarea[placeholder="生成的内容将在此显示..."]').attributes('readonly')).toBeUndefined()
    running.resolve()
    await generate
    expect(setup.generatedContent).toBe('已有片段')
  })

  it('disconnected fragments are protected and recovery reuses the request and cursor', async () => {
    state.consume.mockImplementationOnce(async (_slug, data, handlers) => {
      handlers.onRequest(data.request_id)
      handlers.onChunk('前段')
      handlers.onCursor(2)
      handlers.onError('disconnected')
    }).mockImplementationOnce(async (_slug, _data, handlers) => {
      handlers.onChunk('后段')
      handlers.onDone({ content: '前段后段', token_count: 1, consistency_report: {} })
      handlers.onTerminal('completed')
    })
    const { wrapper, setup } = await open()
    await setup.handleStartGenerate()
    await flushPromises()
    expect(wrapper.text()).toContain('避免恢复事件覆盖本地改动')
    const editor = wrapper.find('textarea[placeholder="生成的内容将在此显示..."]')
    await editor.setValue('本地修改')
    expect(setup.generatedContent).toBe('前段')
    await button(wrapper, '恢复本次生成').trigger('click')
    await flushPromises()
    expect(state.consume.mock.calls[1][1].request_id).toBe(state.consume.mock.calls[0][1].request_id)
    expect(state.consume.mock.calls[1][1].resume_after).toBe(2)
    expect(setup.generatedContent).toBe('前段后段')
    expect(editor.attributes('readonly')).toBeUndefined()
  })

  it('clearing a disconnected draft invalidates its old subscription and recovery action', async () => {
    let callbacks: any
    state.consume.mockImplementation(async (_slug, data, handlers) => { callbacks = handlers; handlers.onRequest(data.request_id); handlers.onChunk('old'); handlers.onError('lost') })
    const { wrapper, setup } = await open()
    await setup.handleStartGenerate()
    await flushPromises()
    await button(wrapper, '清空').trigger('click')
    callbacks.onDone({ content: 'late old text', consistency_report: {}, token_count: 0 })
    await flushPromises()
    expect(setup.generatedContent).toBe('')
    expect(wrapper.findAll('button').some(candidate => candidate.text() === '恢复本次生成')).toBe(false)
  })
})
