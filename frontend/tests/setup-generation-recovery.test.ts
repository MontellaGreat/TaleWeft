import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NovelSetupGuide from '../src/components/onboarding/NovelSetupGuide.vue'
import { useAIInvocationStore } from '../src/stores/aiInvocationStore'
import type { InvocationResponseDTO } from '../src/api/aiInvocation'

const state = vi.hoisted(() => ({
  consume: vi.fn(), getBible: vi.fn(), updateBible: vi.fn(), features: { aiInvocationDebug: false },
  api: { get: vi.fn(), resume: vi.fn(), accept: vi.fn(), commit: vi.fn(), retry: vi.fn() },
}))
vi.mock('../src/config/features', () => ({ featureFlags: state.features }))
vi.mock('../src/api/aiInvocation', () => ({ aiInvocationApi: state.api }))
vi.mock('../src/api/bible', () => ({ consumeBibleGenerateStream: state.consume, bibleApi: { getBible: state.getBible, updateBible: state.updateBible } }))
vi.mock('../src/api/worldbuilding', () => ({ worldbuildingApi: { getWorldbuilding: async () => ({}) } }))
vi.mock('../src/api/workflow', () => ({ consumePlotOutlineStream: vi.fn(), workflowApi: { getPlotOutline: async () => ({ plot_outline: null }) } }))
vi.mock('../src/api/engineCore', () => ({ characterPsycheApi: {} }))
vi.mock('naive-ui', () => ({ useMessage: () => ({ info: vi.fn(), error: vi.fn(), success: vi.fn() }), useDialog: () => ({ warning: vi.fn() }) }))
vi.mock('../src/components/onboarding/BibleLocationsGraphPreview.vue', () => ({ default: { render: () => null } }))

const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', slots.default?.()) } })
const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const stubs: Record<string, unknown> = { NButton: Button, BibleLocationsGraphPreview: true, WizardSkeleton: true }
for (const name of ['NModal', 'NSpace', 'NSteps', 'NStep', 'NAlert', 'NIcon', 'NTag', 'NList', 'NListItem', 'NGrid', 'NGridItem',
  'NCollapse', 'NCollapseItem', 'NScrollbar', 'NSpin', 'NEmpty', 'NDivider', 'NCard', 'NFormItem', 'NInput', 'NInputNumber', 'NSelect', 'NText', 'NDynamicTags']) stubs[name] = Box
let wrappers: VueWrapper[] = []
const stores: ReturnType<typeof useAIInvocationStore>[] = []
function payload(status: string, extra: Partial<InvocationResponseDTO> = {}): InvocationResponseDTO {
  return { session: { id: 'locations-session', operation: 'bible.setup.locations', node_key: 'bible-locations', policy: 'FULL_INTERACTIVE', status }, ...extra }
}
function store() { const result = useAIInvocationStore(); stores.push(result); return result }
async function wizard(step = 3) {
  const wrapper = mount(NovelSetupGuide, { props: { novelId: 'book', show: false }, global: { stubs } })
  wrappers.push(wrapper)
  const setup = (wrapper.vm as any).$.setupState
  setup.currentStep = step
  await flushPromises()
  return { wrapper, setup }
}
beforeEach(() => {
  vi.resetAllMocks()
  state.features.aiInvocationDebug = false
  setActivePinia(createPinia())
  localStorage.clear()
  state.getBible.mockResolvedValue({ novel_id: 'book', world_settings: [], characters: [], locations: [{ id: 'capital', name: '都城', description: '王城' }] })
  state.consume.mockImplementation(async (_id, _stage, handlers) => { handlers.onApprovalRequired('locations-session') })
})
afterEach(async () => {
  stores.splice(0).forEach(item => item.stopGenerationPolling())
  wrappers.splice(0).forEach(item => item.unmount())
  await flushPromises()
  vi.useRealTimers()
})

describe('setup generation failure recovery through the real invocation store', () => {
  it.each(['failed', 'blocked', 'cancelled'])('locations releases busy state on %s and offers generation again', async status => {
    state.api.get.mockResolvedValue(payload(status, { attempt: { id: 'a', session_id: 'locations-session', content: '', status: 'failed', error: '模型连接失败' } }))
    const { wrapper, setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(false)
    expect(setup.locationsError).toBeTruthy()
    expect(wrapper.text()).toContain(setup.locationsError)
    expect(wrapper.findAll('button').some(button => button.text() === '开始生成')).toBe(true)
  })
  it('shows missing location inputs instead of waiting for an impossible automatic review', async () => {
    const blocked = payload('blocked')
    blocked.session.variable_plan = { required_missing: ['characters.list'] }
    state.api.get.mockResolvedValue(blocked)
    const { wrapper, setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(wrapper.text()).toContain('characters.list')
    expect(setup.generatingLocations).toBe(false)
    expect(state.api.resume).not.toHaveBeenCalled()
  })
  it.each(['resume', 'accept', 'commit'] as const)('surfaces headless %s request failure, then succeeds on retry', async action => {
    const status = { resume: 'awaiting_pre_call_review', accept: 'awaiting_acceptance', commit: 'awaiting_commit' }[action]
    const initial = payload(status, {
      attempt: { id: 'a', session_id: 'locations-session', content: '{}', status: 'succeeded' },
      decision: action === 'commit' ? { id: 'd' } as any : null,
    })
    state.api.get.mockResolvedValue(initial)
    state.api[action].mockRejectedValue(new Error(`${action} disconnected`))
    const { wrapper, setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(wrapper.text()).toContain(`${action} disconnected`)
    expect(setup.locationsGenerated).toBe(false)
    state.api.get.mockResolvedValue(payload('completed'))
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.locationsError).toBe('')
    expect(wrapper.text()).toContain('请查看并修改地点设定')
    expect(setup.editableLocations[0].name).toBe('都城')
  })
  it('handles a failed map commit with its real persistence error', async () => {
    state.api.get.mockResolvedValue(payload('failed', { commit: { id: 'c', session_id: 'locations-session', decision_id: 'd', status: 'failed', steps: [], error: '无效的地点父子关系' } }))
    const { wrapper, setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(wrapper.text()).toContain('无效的地点父子关系')
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(false)
  })
  it('updates location cards from incremental attempt content, then displays committed Bible data', async () => {
    vi.useFakeTimers()
    state.api.get
      .mockResolvedValueOnce(payload('generating', {
        attempt: {
          id: 'a',
          session_id: 'locations-session',
          status: 'running',
          // The provider has emitted one complete object but has not closed
          // the array or outer object yet.
          content: '{"locations":[{"id":"harbor_archive","name":"港务署档案室","type":"建筑","description":"存放潮汐记录"}',
        },
      }))
      .mockResolvedValueOnce(payload('generating', {
        attempt: {
          id: 'a',
          session_id: 'locations-session',
          status: 'running',
          // The next persisted snapshot grows the response. Cards are replaced
          // from the snapshot, so the first location is not duplicated.
          content: '{"locations":[{"id":"harbor_archive","name":"港务署档案室","type":"建筑","description":"存放潮汐记录"},{"id":"inspection_well","name":"验潮井","type":"区域","description":"检查水位刻线"},{"name":"',
        },
      }))
      .mockResolvedValueOnce(payload('completed', {
        attempt: { id: 'a', session_id: 'locations-session', status: 'succeeded', content: '{"locations":[]}' },
        decision: { id: 'd' } as any,
        commit: { id: 'c', session_id: 'locations-session', decision_id: 'd', status: 'succeeded', steps: [] },
      }))
    const { setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.generatingLocations).toBe(true)
    expect(setup.locationsGenerated).toBe(false)
    expect(setup.streamingLocations).toEqual([
      expect.objectContaining({
        id: 'harbor_archive',
        name: '港务署档案室',
        description: '存放潮汐记录',
      }),
    ])
    expect(setup.phaseMessage).toContain('1 个地点')

    await vi.advanceTimersByTimeAsync(1200)
    await flushPromises()
    expect(setup.streamingLocations).toHaveLength(2)
    expect(setup.streamingLocations.map((item: { name: string }) => item.name)).toEqual(['港务署档案室', '验潮井'])
    expect(setup.phaseMessage).toContain('2 个地点')

    await vi.advanceTimersByTimeAsync(1200)
    await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.editableLocations).toEqual([
      expect.objectContaining({ id: 'capital', name: '都城' }),
    ])
    expect(state.api.get).toHaveBeenCalledTimes(3)
  })
  it('completes headless review, generation, adoption and persistence before showing the map', async () => {
    state.api.get.mockResolvedValue(payload('awaiting_pre_call_review'))
    state.api.resume.mockResolvedValue(payload('awaiting_acceptance', { attempt: { id: 'a', session_id: 'locations-session', status: 'succeeded', content: '{}' } }))
    state.api.accept.mockResolvedValue(payload('awaiting_commit', { decision: { id: 'd' } as any }))
    state.api.commit.mockResolvedValue(payload('completed'))
    const { setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(state.api.resume).toHaveBeenCalledTimes(1)
    expect(state.api.accept).toHaveBeenCalledTimes(1)
    expect(state.api.commit).toHaveBeenCalledWith('locations-session', 'd')
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.generatingLocations).toBe(false)
  })
  it('worldbuilding errors release the same busy guard as characters and locations', async () => {
    state.consume.mockImplementation(async (_id, _stage, handlers) => { handlers.onError('连接中断') })
    const { setup } = await wizard(1)
    setup.startBibleGenerationSSE()
    await flushPromises()
    expect(setup.bibleError).toBe('连接中断')
    expect(setup.generatingBible).toBe(false)
    expect(setup.isWizardGenerating).toBe(false)
  })
  it('does not report success when persisted map data cannot be loaded', async () => {
    state.api.get.mockResolvedValue(payload('completed'))
    state.getBible.mockRejectedValue(new Error('read offline'))
    const { setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(false)
    expect(setup.locationsError).toContain('读取失败')
  })
  it.each([
    ['startBibleGenerationSSE', 'generatingBible', 'bibleGenerated', 'bibleError'],
    ['startCharactersGenerationSSE', 'generatingCharacters', 'charactersGenerated', 'charactersError'],
    ['startLocationsGenerationSSE', 'generatingLocations', 'locationsGenerated', 'locationsError'],
  ])('requires persisted data after a legacy SSE done event from %s', async (start, busy, generated, error) => {
    state.consume.mockImplementation(async (_id, _stage, handlers) => handlers.onDone('book'))
    state.getBible.mockRejectedValue(new Error('read offline'))
    const { setup } = await wizard()
    setup[start]()
    await flushPromises()
    expect(setup[busy]).toBe(false)
    expect(setup[generated]).toBe(false)
    expect(setup[error]).toContain('读取失败')
  })
  it('polling tolerates a transient outage and still observes completion', async () => {
    vi.useFakeTimers()
    const invocation = store(), onError = vi.fn(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate, onError)
    state.api.get.mockRejectedValueOnce(new Error('transient')).mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('generating'))
    await vi.advanceTimersByTimeAsync(2600)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
    expect(onError).not.toHaveBeenCalled()
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it('reports repeated polling failure to the caller instead of polling silently forever', async () => {
    vi.useFakeTimers()
    const invocation = store(), onError = vi.fn()
    invocation.onSessionUpdate('locations-session', vi.fn(), onError)
    state.api.get.mockRejectedValue(new Error('offline'))
    invocation.openFromResponse(payload('generating'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('无法获取生成进度'))
    expect(state.api.get).toHaveBeenCalledTimes(3)
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it.each(['awaiting_acceptance', 'awaiting_commit'])('rejects an incomplete %s response instead of waiting forever', async status => {
    const invocation = store(), onError = vi.fn()
    invocation.onSessionUpdate('locations-session', vi.fn(), onError)
    invocation.openFromResponse(payload(status))
    await flushPromises()
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('缺少下一步'))
  })
  it('polls an ongoing commit until its authoritative completion', async () => {
    vi.useFakeTimers()
    const invocation = store(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate)
    state.api.get.mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('committing'))
    await vi.advanceTimersByTimeAsync(1300)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it('continues observing generation when the review drawer is closed', async () => {
    state.features.aiInvocationDebug = true
    vi.useFakeTimers()
    const invocation = store(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate)
    state.api.get.mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('generating'))
    invocation.close()
    await vi.advanceTimersByTimeAsync(1300)
    expect(invocation.visible).toBe(false)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
  })
  it('observes a retry of the same failed session from the review drawer', async () => {
    state.features.aiInvocationDebug = true
    vi.useFakeTimers()
    state.api.get.mockResolvedValue(payload('failed', { commit: { id: 'c', status: 'failed', error: '保存失败' } as any }))
    const invocation = store(), { setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    expect(setup.locationsError).toBe('保存失败')
    state.api.retry.mockResolvedValue(payload('generating'))
    await invocation.retry()
    expect(setup.locationsError).toBe('')
    expect(setup.generatingLocations).toBe(true)
    state.api.get.mockResolvedValue(payload('completed'))
    await vi.advanceTimersByTimeAsync(1300)
    await flushPromises()
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.generatingLocations).toBe(false)
  })
  it('keeps generated location parents when confirming the next wizard step', async () => {
    state.api.get.mockResolvedValue(payload('completed'))
    state.getBible.mockResolvedValue({ world_settings: [], characters: [], locations: [
      { id: 'capital', name: '都城', parent_id: null },
      { id: 'palace', name: '王宫', parent_id: 'capital' },
    ] })
    const { setup } = await wizard()
    setup.startLocationsGeneration()
    await flushPromises()
    await setup.handleNext()
    await flushPromises()
    expect(state.updateBible).toHaveBeenCalledWith('book', expect.objectContaining({ locations: [
      expect.objectContaining({ id: 'capital', parent_id: null }),
      expect.objectContaining({ id: 'palace', parent_id: 'capital' }),
    ] }))
    expect(setup.currentStep).toBe(4)
  })
  it('keeps parents on wizard restore and promotes children if their parent is deleted', async () => {
    state.getBible.mockResolvedValue({ world_settings: [], characters: [], locations: [
      { id: 'capital', name: '都城', parent_id: null },
      { id: 'palace', name: '王宫', parent_id: 'capital' },
    ] })
    const { setup } = await wizard()
    await setup.detectWizardProgress()
    expect(setup.editableLocations[1].parent_id).toBe('capital')
    setup.editableLocations.splice(0, 1)
    await setup.saveLocationsEdits()
    expect(state.updateBible).toHaveBeenCalledWith('book', expect.objectContaining({ locations: [
      expect.objectContaining({ id: 'palace', parent_id: null }),
    ] }))
  })
  it.each(['blocked', 'failed'])('does not adopt rejected plot JSON from a %s commit', async status => {
    const { setup } = await wizard(4)
    await setup.handlePlotOutlineInvocationUpdate(payload(status, { commit: {
      id: 'c', status: 'failed', error: '总纲阶段校验失败',
      result: { accepted_content: JSON.stringify({ main_story_overview: '主线', stage_plan: [{ phase: 'opening', summary: '非法单阶段' }] }) },
    } as any }))
    expect(setup.plotOutlineCommitted).toBe(false)
    expect(setup.plotOutlineStatus).toBe('error')
    expect(setup.plotOutlineError).toBe('总纲阶段校验失败')
    expect(setup.plotOutlineGenerating).toBe(false)
  })
})
