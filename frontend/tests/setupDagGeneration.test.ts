import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NovelSetupGuide from '@/components/onboarding/NovelSetupGuide.vue'
import { useDAGStore } from '@/stores/dagStore'
import DAGCanvas from '@/components/autopilot/DAGCanvas.vue'

const state = vi.hoisted(() => ({
  catalog: vi.fn(), start: vi.fn(), getRun: vi.fn(), stop: vi.fn(),
  getBible: vi.fn(), getWorld: vi.fn(), getPlot: vi.fn(), consume: vi.fn(), updateBible: vi.fn(),
  warning: vi.fn(),
}))
vi.mock('@/api/config', () => ({ apiClient: {
  get: (url: string) => url.includes('/runs/') ? state.getRun(url) : state.catalog(url),
  post: (url: string, body: unknown) => url.endsWith('/stop') ? state.stop(url, body) : state.start(url, body),
} }))
vi.mock('@/config/features', () => ({ featureFlags: { aiInvocationDebug: false } }))
vi.mock('@/api/bible', () => ({ consumeBibleGenerateStream: state.consume, bibleApi: { getBible: state.getBible, updateBible: state.updateBible } }))
vi.mock('@/api/worldbuilding', () => ({ worldbuildingApi: { getWorldbuilding: state.getWorld } }))
vi.mock('@/api/workflow', () => ({ consumePlotOutlineStream: state.consume, workflowApi: { getPlotOutline: state.getPlot } }))
vi.mock('@/api/engineCore', () => ({ characterPsycheApi: {} }))
vi.mock('naive-ui', () => ({ useMessage: () => ({ info: vi.fn(), error: vi.fn(), success: vi.fn() }), useDialog: () => ({ warning: state.warning }) }))
vi.mock('@/components/onboarding/BibleLocationsGraphPreview.vue', () => ({ default: { render: () => null } }))

const Box = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const Button = defineComponent({ props: ['disabled', 'loading'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled || props.loading, onClick: () => emit('click') }, slots.default?.())
} })
const Progress = defineComponent({ props: ['percentage'], setup(props) { return () => h('progress', { max: 100, value: props.percentage }) } })
const Flow = defineComponent({ name: 'VueFlow', props: ['nodes', 'edges', 'nodesDraggable', 'nodesConnectable'], setup() { return () => h('div', { class: 'flow-test-canvas' }) } })
const stubs: Record<string, unknown> = { NButton: Button, NProgress: Progress, VueFlow: Flow, Background: true, Controls: true, BibleLocationsGraphPreview: true, WizardSkeleton: true }
for (const name of ['NModal', 'NSpace', 'NSteps', 'NStep', 'NAlert', 'NIcon', 'NTag', 'NList', 'NListItem', 'NGrid', 'NGridItem',
  'NCollapse', 'NCollapseItem', 'NScrollbar', 'NSpin', 'NEmpty', 'NDivider', 'NCard', 'NFormItem', 'NInput', 'NInputNumber', 'NSelect', 'NText', 'NDynamicTags']) stubs[name] = Box

const stageIds = ['worldbuilding', 'characters', 'locations', 'plot_outline']
const labels = ['文风 / 世界观', '人物', '地图', '剧情总纲']
function graph(stage: string) {
  return { nodes: ['prepare', 'generate', 'verify'].map((name, i) => ({ id: `${stage}_${name}`, type: name, label: `${stage} ${name}`, position: { x: i * 200, y: 80 }, config: {} })),
    edges: [{ id: `${stage}-1`, source: `${stage}_prepare`, target: `${stage}_generate` }, { id: `${stage}-2`, source: `${stage}_generate`, target: `${stage}_verify` }] }
}
function run(status = 'running', stage = 'locations', percent = 33, id = 'run-1') {
  return { run_id: id, novel_id: 'book', stage, status, workflow_version: 3, graph: graph(stage), node_states: {
    [`${stage}_prepare`]: { status: 'success' }, [`${stage}_generate`]: { status: status === 'error' ? 'error' : 'running' },
  }, progress: { completed: percent === 100 ? 3 : 1, total: 3, percent, current_node_id: `${stage}_generate`, current_node_label: '正在生成地点', message: '模型正在响应' },
  error: status === 'error' ? '地点父子关系校验失败' : null, started_at: '2026-10-23T16:00:00Z', completed_at: status === 'running' ? null : '2026-10-23T16:00:05Z' }
}
function catalog(active: ReturnType<typeof run> | null = null) {
  return { stages: stageIds.map((id, i) => ({ id, label: labels[i], dependencies: i ? [stageIds[i - 1]] : [], graph: graph(id), latest_run: active?.stage === id ? active : null })), active_run: active }
}
let wrapper: VueWrapper
async function wizard(step = 3, show = false) {
  wrapper = mount(NovelSetupGuide, { props: { novelId: 'book', show }, global: { stubs } })
  await flushPromises()
  const setup = (wrapper.vm as any).$.setupState
  if (!show) setup.currentStep = step
  return setup
}
function button(label: string) { return wrapper.findAll('button').find(item => item.text() === label)! }
beforeEach(() => {
  vi.resetAllMocks(); setActivePinia(createPinia()); localStorage.clear()
  state.catalog.mockResolvedValue(catalog())
  state.start.mockResolvedValue(run())
  state.getRun.mockResolvedValue(run())
  state.stop.mockResolvedValue(run('cancelled'))
  state.getPlot.mockResolvedValue({ plot_outline: null })
  state.getWorld.mockResolvedValue({})
  state.getBible.mockResolvedValue({ novel_id: 'book', world_settings: [], characters: [], locations: [{ id: 'capital', name: '都城', description: '王城' }], style_notes: [] })
})
afterEach(() => { wrapper?.unmount(); vi.useRealTimers() })

describe('wizard setup DAG execution', () => {
  it.each([
    ['startBibleGeneration', 'worldbuilding'], ['startCharactersGeneration', 'characters'], ['startLocationsGeneration', 'locations'], ['loadPlotOutline', 'plot_outline'],
  ])('runs %s through the setup DAG endpoint', async (entry, stage) => {
    const setup = await wizard()
    state.start.mockResolvedValue(run('running', stage))
    await setup[entry](); await flushPromises()
    expect(state.start).toHaveBeenCalledWith('/dag/book/setup/run', { stage })
    expect(state.consume).not.toHaveBeenCalled()
  })

  it('displays authoritative progress and marks generated only after completed data is read back', async () => {
    vi.useFakeTimers()
    const setup = await wizard()
    state.getRun.mockResolvedValueOnce(run('running', 'locations', 66)).mockResolvedValueOnce(run('completed', 'locations', 100))
    await setup.startLocationsGeneration(); await flushPromises()
    expect(wrapper.get('progress').attributes('value')).toBe('33')
    expect(wrapper.text()).toContain('正在生成地点')
    expect(setup.locationsGenerated).toBe(false)
    await vi.advanceTimersByTimeAsync(1200)
    expect(wrapper.get('progress').attributes('value')).toBe('66')
    await vi.advanceTimersByTimeAsync(1200); await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.editableLocations[0].name).toBe('都城')
    expect(wrapper.get('progress').attributes('value')).toBe('100')
  })

  it.each(['error', 'cancelled', 'interrupted'])('releases the busy state after %s and allows a new run', async status => {
    const setup = await wizard()
    state.start.mockResolvedValueOnce(run(status))
    await setup.startLocationsGeneration(); await flushPromises()
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(false)
    expect(setup.locationsError).toBeTruthy()
    expect(wrapper.text()).toContain(setup.locationsError)
    state.start.mockResolvedValue(run('completed', 'locations', 100, 'retry-run'))
    await setup.startLocationsGeneration(); await flushPromises()
    expect(state.start).toHaveBeenCalledTimes(2)
    expect(setup.locationsGenerated).toBe(true)
  })

  it('does not mark data generated when the post-completion read fails', async () => {
    const setup = await wizard()
    state.start.mockResolvedValue(run('completed', 'locations', 100))
    state.getBible.mockRejectedValue(new Error('read offline'))
    await setup.startLocationsGeneration(); await flushPromises()
    expect(setup.locationsGenerated).toBe(false)
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsError).toContain('读取失败')
  })

  it('does not mark worldbuilding generated when its persisted world data cannot be read', async () => {
    const setup = await wizard(1)
    state.start.mockResolvedValue(run('completed', 'worldbuilding', 100))
    state.getWorld.mockRejectedValue(new Error('world data offline'))
    await setup.startBibleGeneration(); await flushPromises()
    expect(setup.bibleGenerated).toBe(false)
    expect(setup.generatingBible).toBe(false)
    expect(setup.bibleError).toContain('读取失败')
  })

  it('uses the backend plot nodes for the stage timeline', async () => {
    const setup = await wizard(4)
    state.start.mockResolvedValue(run('running', 'plot_outline', 66))
    await setup.loadPlotOutline(); await flushPromises()
    expect(wrapper.text()).toContain('plot_outline verify')
    expect(wrapper.text()).toContain('plot_outline prepare')
  })

  it('recovers a running task on reopen and closes the view without stopping the backend', async () => {
    vi.useFakeTimers()
    state.catalog.mockResolvedValue(catalog(run()))
    const setup = await wizard(1, true)
    expect(setup.currentStep).toBe(3)
    expect(setup.generatingLocations).toBe(true)
    expect(state.start).not.toHaveBeenCalled()
    await wrapper.setProps({ show: false }); await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(state.getRun).not.toHaveBeenCalled()
    expect(state.stop).not.toHaveBeenCalled()
    await wrapper.setProps({ show: true }); await flushPromises()
    expect(setup.generatingLocations).toBe(true)
    await vi.advanceTimersByTimeAsync(1200)
    expect(state.getRun).toHaveBeenCalledWith('/dag/book/setup/runs/run-1')
    expect(state.start).not.toHaveBeenCalled()
  })

  it('does not apply an obsolete completed read after the view reopens on another run', async () => {
    let resolve!: (value: unknown) => void
    const setup = await wizard(1, true)
    state.start.mockResolvedValue(run('completed', 'locations', 100))
    state.getBible.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const starting = setup.startLocationsGeneration()
    await flushPromises()
    await wrapper.setProps({ show: false }); await flushPromises()
    state.catalog.mockResolvedValue(catalog(run('running', 'characters', 33, 'new-run')))
    await wrapper.setProps({ show: true }); await flushPromises()
    resolve({ novel_id: 'book', world_settings: [], characters: [], locations: [{ id: 'old', name: '过期地点' }], style_notes: [] })
    await starting; await flushPromises()
    expect(setup.currentStep).toBe(2)
    expect(setup.generatingCharacters).toBe(true)
    expect(setup.editableLocations[0].name).toBe('都城')
  })

  it('stops only on an explicit stop action and displays the confirmed result', async () => {
    const setup = await wizard()
    await setup.startLocationsGeneration(); await flushPromises()
    await button('停止生成').trigger('click'); await flushPromises()
    expect(state.stop).toHaveBeenCalledWith('/dag/book/setup/stop', { run_id: 'run-1' })
    expect(setup.generatingLocations).toBe(false)
    expect(setup.locationsGenerated).toBe(false)
  })

  it('ignores a pending progress response after an explicit stop has completed', async () => {
    vi.useFakeTimers()
    let resolve!: (value: ReturnType<typeof run>) => void
    state.getRun.mockReturnValue(new Promise(done => { resolve = done }))
    const setup = await wizard()
    await setup.startLocationsGeneration(); await flushPromises()
    await vi.advanceTimersByTimeAsync(1200)
    await button('停止生成').trigger('click'); await flushPromises()
    resolve(run('completed', 'locations', 100)); await flushPromises()
    expect(setup.locationsGenerated).toBe(false)
    expect(wrapper.text()).toContain('已停止')
  })

  it('shows polling failure explicitly and reconnects without starting a duplicate run', async () => {
    vi.useFakeTimers()
    const setup = await wizard()
    state.getRun.mockRejectedValue(new Error('offline'))
    await setup.startLocationsGeneration(); await flushPromises()
    await vi.advanceTimersByTimeAsync(4000)
    expect(wrapper.text()).toContain('无法获取生成进度')
    expect(setup.isWizardGenerating).toBe(true)
    state.catalog.mockResolvedValue(catalog(run('completed', 'locations', 100)))
    await button('重新连接').trigger('click'); await flushPromises()
    expect(setup.locationsGenerated).toBe(true)
    expect(state.start).toHaveBeenCalledTimes(1)
  })

  it('shows all stage dependencies and highlights the running node in a read only DAG', async () => {
    state.catalog.mockResolvedValue(catalog(run()))
    await wizard(1, true)
    for (const label of labels) expect(wrapper.text()).toContain(label)
    await button('查看 DAG 工作流').trigger('click'); await flushPromises()
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.exists()).toBe(true)
    expect(flow.props('nodesDraggable')).toBe(false)
    expect(flow.props('nodesConnectable')).toBe(false)
    expect(wrapper.findComponent(DAGCanvas).exists()).toBe(true)
    expect(flow.props('nodes')).toHaveLength(3)
    expect(flow.props('edges')).toEqual(expect.arrayContaining([expect.objectContaining({ source: 'locations_prepare', target: 'locations_generate' })]))
    expect(flow.props('nodes').find((node: any) => node.id === 'locations_generate').class).toContain('is-current')
    expect(flow.props('nodes').every((node: any) => node.type === 'dagCustom')).toBe(true)
  })

  it('keeps generated location parents when confirming the next step', async () => {
    state.start.mockResolvedValue(run('completed', 'locations', 100))
    state.getBible.mockResolvedValue({ world_settings: [], characters: [], locations: [
      { id: 'capital', name: '都城', parent_id: null }, { id: 'palace', name: '王宫', parent_id: 'capital' },
    ] })
    const setup = await wizard()
    await setup.startLocationsGeneration(); await flushPromises()
    await setup.handleNext(); await flushPromises()
    expect(state.updateBible).toHaveBeenCalledWith('book', expect.objectContaining({ locations: [
      expect.objectContaining({ id: 'capital', parent_id: null }), expect.objectContaining({ id: 'palace', parent_id: 'capital' }),
    ] }))
    expect(setup.currentStep).toBe(4)
  })

  it('keeps parents on restore and promotes children when their parent is deleted', async () => {
    state.getBible.mockResolvedValue({ world_settings: [], characters: [], locations: [
      { id: 'capital', name: '都城', parent_id: null }, { id: 'palace', name: '王宫', parent_id: 'capital' },
    ] })
    const setup = await wizard()
    await setup.detectWizardProgress()
    expect(setup.editableLocations[1].parent_id).toBe('capital')
    setup.editableLocations.splice(0, 1)
    await setup.saveLocationsEdits()
    expect(state.updateBible).toHaveBeenCalledWith('book', expect.objectContaining({ locations: [expect.objectContaining({ id: 'palace', parent_id: null })] }))
  })

  it('does not adopt a completed plot outline when its persisted result is unavailable', async () => {
    const setup = await wizard(4)
    state.start.mockResolvedValue(run('completed', 'plot_outline', 100))
    state.getPlot.mockRejectedValue(new Error('read offline'))
    await setup.loadPlotOutline(); await flushPromises()
    expect(setup.plotOutlineCommitted).toBe(false)
    expect(setup.plotOutlineGenerating).toBe(false)
    expect(setup.plotOutlineError).toContain('读取失败')
    expect(button('继续生成')).toBeDefined()
    await button('继续生成').trigger('click'); await flushPromises()
    expect(state.start).toHaveBeenCalledTimes(2)
  })

  it('locks failed stage data on reopen and blocks save, skip, and forward navigation', async () => {
    localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: 5 }))
    state.catalog.mockResolvedValue(catalog(run('error')))
    const setup = await wizard(1, true)
    expect(setup.currentStep).toBe(3)
    expect(setup.currentStageLocked).toBe(true)
    expect(wrapper.findAllComponents({ name: 'NInput' })).toHaveLength(0)
    expect(wrapper.text()).toContain('暂不可编辑或进入下一步')
    await setup.handleNext()
    setup.goToStep(4)
    setup.handleSkip()
    setup.handleComplete()
    expect(await setup.saveLocationsEdits()).toBe(false)
    expect(setup.currentStep).toBe(3)
    expect(state.updateBible).not.toHaveBeenCalled()
    expect(state.warning).not.toHaveBeenCalled()
    expect(wrapper.emitted('complete')).toBeUndefined()
    expect(wrapper.emitted('skip')).toBeUndefined()
    expect(button('继续生成').attributes('disabled')).toBeUndefined()
  })

  it('requires an explicit confirmation before restarting all stage steps', async () => {
    const setup = await wizard()
    state.start.mockResolvedValue(run('error'))
    await setup.startLocationsGeneration(); await flushPromises()
    await button('从头重新生成').trigger('click')
    expect(state.start).toHaveBeenCalledTimes(1)
    expect(state.warning).toHaveBeenCalledWith(expect.objectContaining({ title: '从头重新生成' }))
    state.start.mockResolvedValue(run('running'))
    await state.warning.mock.calls[0][0].onPositiveClick()
    expect(state.start).toHaveBeenLastCalledWith('/dag/book/setup/run', { stage: 'locations', restart: true })
  })

  it.each([['characters', '人物', 2], ['locations', '地图', 3], ['plot_outline', '剧情总纲', 4]])('keeps prerequisites read only while %s is unfinished and routes back to that stage', async (stage, label, step) => {
    const data = catalog(run('error', String(stage)))
    data.stages.forEach((item, index) => {
      if (index < Number(step) - 1) item.latest_run = run('completed', item.id, 100, `${item.id}-complete`)
    })
    state.catalog.mockResolvedValue(data)
    state.getBible.mockResolvedValue({ novel_id: 'book', world_settings: [], characters: [{ id: 'hero', name: '主角' }], locations: [{ id: 'capital', name: '都城' }], style_notes: [{ content: '已审核文风' }] })
    const setup = await wizard(1, true)
    expect(setup.currentStep).toBe(step)
    setup.goToStep(1)
    await flushPromises()
    expect(setup.currentStageIncomplete).toBe(false)
    expect(setup.currentDependencyLock).toBe(stage)
    expect(setup.currentStageLocked).toBe(true)
    expect(setup.stageStartLabel('worldbuilding')).toBe('开始生成')
    expect(wrapper.text()).toContain(`请先继续${label}的审核与保存`)
    expect(wrapper.find('.saved-stage-preview').exists()).toBe(true)
    expect(wrapper.find('.bible-preview').exists()).toBe(false)
    expect(button('开始生成')).toBeUndefined()
    expect(button('继续生成')).toBeUndefined()
    expect(button('重新生成')).toBeUndefined()
    expect(button('从头重新生成')).toBeUndefined()
    expect(await setup.saveWorldbuildingEdits()).toBe(false)
    await setup.startBibleGeneration()
    setup.restartSetupStage('worldbuilding')
    await setup.handleNext()
    expect(state.start).not.toHaveBeenCalled()
    expect(state.updateBible).not.toHaveBeenCalled()
    expect(state.warning).not.toHaveBeenCalled()
    await button(`前往${label}继续`).trigger('click')
    expect(setup.currentStep).toBe(step)
    expect(setup.currentStageIncomplete).toBe(true)
    expect(setup.currentDependencyLock).toBe(null)
  })

  it('shows canonical saved units while further world audits run, then resumes without unlocking', async () => {
    const units = ['style', 'core_rules', 'geography', 'society', 'culture', 'daily_life', 'world_review']
    const actions = ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
    const nodes = units.flatMap(unit => (unit === 'world_review' ? ['prepare', 'validate', 'deviation', 'anti_ai', 'commit', 'verify'] : actions)
      .map((action, i) => ({ id: `worldbuilding_${unit}_${action}`, type: 'setup_step', label: `${unit} ${action}`, position: { x: i * 200, y: 80 }, config: { prompt_variables: { unit, action } } })))
    const partial = { ...run('running', 'worldbuilding', 29), graph: { nodes, edges: [] },
      progress: { completed: 14, total: 48, percent: 29, current_node_id: nodes[14]!.id, current_node_label: '地理生态 · 准备上下文', message: '已保存文风与核心法则，继续构建地理生态' },
      node_states: Object.fromEntries(nodes.map((node, i) => [node.id, { status: i < 14 ? 'success' : i === 14 ? 'running' : 'pending', outputs: { draft: { style: '未经审核的候选' } } }])) }
    state.start.mockResolvedValue(partial)
    state.getBible.mockResolvedValue({ world_settings: [], characters: [], locations: [], style_notes: [{ content: '已审核保存的文风' }] })
    const setup = await wizard(1)
    await setup.startBibleGeneration(); await flushPromises()
    expect(setup.completedDimensions.has('core_rules')).toBe(true)
    expect(setup.currentStageLocked).toBe(true)
    expect(wrapper.text()).toContain('已审核保存的文风')
    expect(wrapper.text()).not.toContain('未经审核的候选')
    expect(wrapper.text()).toContain('审核通过，已保存并回读')
    await button('查看 DAG 工作流').trigger('click'); await flushPromises()
    expect(wrapper.findComponent({ name: 'VueFlow' }).props('nodes')).toHaveLength(48)
    state.start.mockResolvedValue({ ...partial, status: 'error', error: '地理偏离审核失败' })
    state.stop.mockResolvedValue({ ...partial, status: 'cancelled', error: null })
    await setup.setupDag.stop(); await flushPromises()
    await setup.startBibleGeneration(); await flushPromises()
    expect(setup.currentStageLocked).toBe(true)
    expect(setup.isWizardGenerating).toBe(false)
    await button('继续生成').trigger('click'); await flushPromises()
    expect(state.start).toHaveBeenLastCalledWith('/dag/book/setup/run', { stage: 'worldbuilding' })
  })

  it('keeps the writing canvas definition and node selection isolated from setup graphs', async () => {
    const store = useDAGStore()
    store.dagDefinition = { id: 'writing', name: '写作', version: 1, description: '', nodes: [], edges: [], metadata: { created_at: '', updated_at: '', created_by: '' } }
    store.selectedNodeId = 'writing-node'
    const before = JSON.stringify(store.dagDefinition)
    state.catalog.mockResolvedValue(catalog(run()))
    await wizard(1, true)
    await button('查看 DAG 工作流').trigger('click'); await flushPromises()
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    flow.vm.$emit('nodeClick', { node: { id: 'locations_generate' } })
    flow.vm.$emit('nodeContextMenu', { node: { id: 'locations_generate' } })
    expect(store.selectedNodeId).toBe('writing-node')
    expect(JSON.stringify(store.dagDefinition)).toBe(before)
    wrapper.findComponent(DAGCanvas).vm.$emit('update:nodes', [])
    expect(JSON.stringify(store.dagDefinition)).toBe(before)
  })

  it.each([1, undefined])('keeps existing book drafts editable after a legacy workflow version %s failure', async version => {
    localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: 3 }))
    const legacy = { ...run('error'), workflow_version: version }
    state.catalog.mockResolvedValue(catalog(legacy as ReturnType<typeof run>))
    const setup = await wizard(1, true)
    setup.goToStep(3)
    expect(setup.currentStep).toBe(3)
    expect(setup.currentStageLocked).toBe(false)
    expect(setup.locationsGenerated).toBe(true)
    expect(setup.editableLocations[0].name).toBe('都城')
    expect(setup.locationsError).toBe('')
  })

  it('preserves the writing canvas store mode and clones positions before VueFlow edits them', async () => {
    const store = useDAGStore()
    store.dagDefinition = { id: 'writing', name: '写作', version: 1, description: '',
      nodes: [{ id: 'writer', type: 'exec_writer', label: '创作', position: { x: 20, y: 40 }, enabled: true, config: {} }],
      edges: [], metadata: { created_at: '', updated_at: '', created_by: '' } }
    wrapper = mount(DAGCanvas, { props: { novelId: 'book' }, global: { stubs } })
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.props('nodes')).toHaveLength(1)
    flow.props('nodes')[0].position.x = 999
    expect(store.dagDefinition.nodes[0]!.position.x).toBe(20)
    flow.vm.$emit('nodeClick', { node: { id: 'writer' } })
    expect(store.selectedNodeId).toBe('writer')
    expect(wrapper.emitted('nodeDetail')).toEqual([['writer']])
  })
})
