import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SetupDAGProgress from '@/components/onboarding/SetupDAGProgress.vue'
import DAGCanvas from '@/components/autopilot/DAGCanvas.vue'
import { useDAGStore } from '@/stores/dagStore'
import { inspectionCatalog, inspectionGraph, inspectionRun } from './fixtures/setupDagInspection'
import SetupDAGAuditReport from '@/components/onboarding/SetupDAGAuditReport.vue'

const Box = defineComponent({ props: ['title'], setup(props, { slots }) { return () => h('section', [props.title ? h('h2', props.title) : null, slots.default?.()]) } })
const Button = defineComponent({ setup(_, { slots, attrs }) { return () => h('button', attrs, slots.default?.()) } })
const Flow = defineComponent({ name: 'VueFlow', props: ['nodes', 'edges'], setup() { return () => h('div') } })
const stubs: Record<string, unknown> = { VueFlow: Flow, Background: true, Controls: true, MiniMap: true, NButton: Button }
for (const name of ['NModal', 'NSpace', 'NAlert', 'NProgress', 'NEmpty', 'NTag', 'NCollapse', 'NCollapseItem']) stubs[name] = Box
let wrapper: VueWrapper
beforeEach(() => { setActivePinia(createPinia()) })
afterEach(() => wrapper?.unmount())
async function progress(legacy = false, currentStage = 'worldbuilding') {
  const stages = inspectionCatalog(legacy)
  wrapper = mount(SetupDAGProgress, { props: { novelId: 'book', stages, run: stages[2]!.latest_run, busy: false, loading: false, stopping: false, error: '', currentStage }, global: { stubs } })
  return stages
}
function button(label: string) { return wrapper.findAll('button').find(item => item.text() === label)! }
function flow() { return wrapper.findComponent({ name: 'VueFlow' }) }
async function open() { await button('查看 DAG 工作流').trigger('click'); await flushPromises() }
async function expand(label: string) {
  const summary = wrapper.findAll('summary').find(item => item.text() === label)!
  expect(summary, label).toBeTruthy()
  const details = summary.element.parentElement as HTMLDetailsElement
  details.open = true
  details.dispatchEvent(new Event('toggle'))
  await flushPromises()
}

describe('setup DAG inspection', () => {
  it('shows the active audit cooldown in progress and node details, then removes it when stopped', async () => {
    const stages = await progress()
    const run = stages[1]!.latest_run!
    run.status = 'running'
    run.progress.current_node_id = 'characters_anti_ai'
    run.node_states.characters_anti_ai!.status = 'running'
    run.checkpoint!.units!.characters!.model_call_progress = { purpose: 'anti_ai_semantic_review', status: 'retry_wait', call_number: 1, max_calls: 2, retry_at: Date.now() / 1000 + 120 }
    await wrapper.setProps({ run, busy: true, currentStage: 'characters' })
    expect(wrapper.text()).toContain('AI 味语义审稿：上游暂时未响应')
    await open()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_anti_ai'); await flushPromises()
    expect(wrapper.find('.setup-node-detail').text()).toContain('候选内容已保留，可停止生成')
    await wrapper.setProps({ run: { ...run, status: 'cancelled', node_states: { ...run.node_states, characters_anti_ai: { status: 'interrupted' } } }, busy: false })
    expect(wrapper.findAll('.setup-model-call-status')).toHaveLength(0)
  })

  it('shows an identical node and checkpoint audit once and leaves candidate and raw output unmounted', async () => {
    const stages = await progress()
    const run = stages[1]!.latest_run!
    const same = JSON.parse(JSON.stringify(run.node_states.characters_anti_ai!.outputs!.anti_ai_audit))
    run.checkpoint!.units!.characters!.audits!.anti_ai = Object.fromEntries(Object.entries(same).reverse())
    await wrapper.findAll('.setup-dag-stage')[1]!.trigger('click'); await flushPromises()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_anti_ai'); await flushPromises()
    expect(wrapper.findAllComponents(SetupDAGAuditReport)).toHaveLength(1)
    expect(wrapper.find('.setup-node-detail').findAll('pre')).toHaveLength(0)
    await expand('该单元当前候选')
    await expand('检查点当前审核（可能晚于本节点）')
    expect(wrapper.text()).toContain('与本节点报告相同，已在上方展示')
    expect(wrapper.findAllComponents(SetupDAGAuditReport)).toHaveLength(1)
  })

  it('does not mount unrelated audit reports when opening a prepare node', async () => {
    await progress()
    await wrapper.findAll('.setup-dag-stage')[1]!.trigger('click'); await flushPromises()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_prepare'); await flushPromises()
    expect(wrapper.findAllComponents(SetupDAGAuditReport)).toHaveLength(0)
    expect(wrapper.find('.setup-node-detail').findAll('pre')).toHaveLength(0)
    await expand('执行输出与回执')
    await expand('节点实际输出')
    expect(wrapper.text()).toContain('characters_prepare-actual-output')
  })

  it('keeps a different checkpoint report even if its input hash matches the node report', async () => {
    const stages = await progress()
    const run = stages[1]!.latest_run!
    const report = JSON.parse(JSON.stringify(run.node_states.characters_anti_ai!.outputs!.anti_ai_audit))
    Object.assign(report, { passed: false, error: '更新后的语义审稿失败' })
    run.checkpoint!.units!.characters!.audits!.anti_ai = report
    await wrapper.findAll('.setup-dag-stage')[1]!.trigger('click'); await flushPromises()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_anti_ai'); await flushPromises()
    await expand('该单元当前候选')
    await expand('检查点当前审核（可能晚于本节点）')
    await expand('Anti-AI · 未通过')
    expect(wrapper.findAllComponents(SetupDAGAuditReport)).toHaveLength(2)
    expect(wrapper.text()).toContain('更新后的语义审稿失败')
  })
  it('opens the wizard current stage and never hides new audits behind a legacy graph', async () => {
    await progress(true)
    expect(wrapper.findAll('.setup-dag-stage')[0]!.classes()).toContain('is-active')
    expect(wrapper.findAll('.setup-dag-stage')[2]!.classes()).not.toContain('is-active')
    await open()
    expect(flow().props('nodes')).toHaveLength(48)
    expect(wrapper.text()).toContain('文风 / 世界观 · 新书设置 DAG 工作流')
    expect(wrapper.text()).toContain('旧版记录不含新增审核')
    expect(flow().props('nodes').every((node: any) => node.data.runState.status === 'pending')).toBe(true)
    await button('查看历史记录').trigger('click'); await flushPromises()
    expect(flow().props('nodes')).toHaveLength(5)
    expect(flow().props('nodes')[0].data.runState.status).toBe('success')
  })

  it('uses clickable stage cards and keeps reports and titles scoped to the selected stage', async () => {
    await progress()
    expect(wrapper.findAll('.setup-dag-stage').every(item => item.element.tagName === 'BUTTON')).toBe(true)
    await wrapper.findAll('.setup-dag-stage')[1]!.trigger('click'); await flushPromises()
    expect(flow().props('nodes')).toHaveLength(7)
    expect(wrapper.text()).toContain('人物 · 新书设置 DAG 工作流')
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_deviation'); await flushPromises()
    await expand('该单元当前候选')
    await expand('查看当前候选内容')
    await expand('执行输出与回执')
    await expand('执行回执')
    await expand('检查点当前审核（可能晚于本节点）')
    await expand('偏离检测 · 已记录通过结论')
    await expand('审核历史与修复次数（2 条）')
    await expand('第 1 条审核记录')
    expect(wrapper.text()).toContain('characters-characters-deviation-node-report')
    expect(wrapper.text()).toContain('characters-characters-deviation-current-report')
    expect(wrapper.text()).toContain('characters-characters-current-candidate')
    expect(wrapper.text()).toContain('characters_deviation-receipt')
    expect(wrapper.text()).toContain('characters-characters-repair-reason')
    expect(wrapper.text()).toContain('不代表本节点执行时的候选')
    await button('地图').trigger('click'); await flushPromises()
    expect(wrapper.find('.setup-node-detail').exists()).toBe(false)
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'locations_anti_ai'); await flushPromises()
    expect(wrapper.find('[aria-label="本节点的审核报告"]').findComponent(SetupDAGAuditReport).props('report').scan_id).toBe('locations-locations-anti-ai-node-report')
    expect(wrapper.text()).not.toContain('characters-characters-deviation-node-report')
    expect(wrapper.text()).toContain('地图 · 新书设置 DAG 工作流')
  })

  it('opens pending node details without inventing an audit report when no run exists', async () => {
    await progress()
    await wrapper.setProps({ stages: inspectionCatalog().map(stage => ({ ...stage, latest_run: null })), run: null, currentStage: 'plot_outline' })
    await open()
    expect(flow().props('nodes')).toHaveLength(7)
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'plot_outline_anti_ai'); await flushPromises()
    expect(wrapper.text()).toContain('尚未执行')
    expect(wrapper.text()).toContain('暂无本节点的审核报告')
    expect(wrapper.text()).not.toContain('审核通过')
  })

  it('emits external graph node details while keeping writing selection intact', async () => {
    const store = useDAGStore()
    store.selectedNodeId = 'writer'
    const graph = inspectionGraph('characters')
    wrapper = mount(DAGCanvas, { props: { novelId: 'book', nodes: graph.nodes, edges: graph.edges }, global: { stubs } })
    flow().vm.$emit('nodeClick', { node: { id: 'characters_deviation' } })
    expect(wrapper.emitted('nodeDetail')).toEqual([['characters_deviation']])
    expect(store.selectedNodeId).toBe('writer')
  })

  it('preserves normal writing node selection and details', () => {
    const store = useDAGStore()
    wrapper = mount(DAGCanvas, { props: { novelId: 'book' }, global: { stubs } })
    flow().vm.$emit('nodeClick', { node: { id: 'writer' } })
    expect(wrapper.emitted('nodeDetail')).toEqual([['writer']])
    expect(store.selectedNodeId).toBe('writer')
  })

  it('opens each world unit audit from a readable shortcut without requiring a zoomed node click', async () => {
    await progress()
    await open()
    const shortcut = wrapper.find('[aria-label="文风公约 · 偏离检测"]')
    expect(shortcut.exists()).toBe(true)
    await shortcut.trigger('click'); await flushPromises()
    expect(wrapper.find('.setup-node-detail').text()).toContain('worldbuilding-style-deviation-node-report')
  })

  it('shows failed checkpoint audits separately when the failed node has no output', async () => {
    const stages = await progress()
    const run = stages[1]!.latest_run!
    run.node_states.characters_anti_ai = { status: 'error', error: '扫描未完成，执行失败' }
    run.checkpoint!.units!.characters!.audits!.anti_ai = { passed: false, scan_status: 'failed', error: '真实失败报告', input_hash: 'characters-characters-current-hash' }
    await wrapper.findAll('.setup-dag-stage')[1]!.trigger('click'); await flushPromises()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'characters_anti_ai'); await flushPromises()
    const actual = wrapper.find('[aria-label="本节点的审核报告"]')
    expect(actual.text()).toContain('暂无本节点的审核报告')
    expect(actual.text()).not.toContain('真实失败报告')
    expect(wrapper.text()).toContain('真实失败报告')
    expect(wrapper.text()).toContain('扫描未完成，执行失败')
    expect(wrapper.text()).toContain('检查点当前审核（可能晚于本节点）')
  })

  it('clears a selected node when a different run replaces the selected stage', async () => {
    const stages = await progress()
    await open()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'worldbuilding_style_deviation'); await flushPromises()
    expect(wrapper.text()).toContain('worldbuilding-style-deviation-node-report')
    await wrapper.setProps({ stages: stages.map(stage => stage.id === 'worldbuilding' ? { ...stage, latest_run: { ...inspectionRun('worldbuilding'), run_id: 'replacement-run' } } : stage) })
    expect(wrapper.find('.setup-node-detail').exists()).toBe(false)
  })

  it('labels candidates superseded by full world review as history and shows the approved replacement', async () => {
    const stages = await progress()
    const run = stages[0]!.latest_run!
    const record = run.checkpoint!.units!.style!
    Object.assign(record, { superseded_by: 'world_review', revision_commit_id: 'review-commit', approved_value_hash: 'approved-style-hash', validated_hash: 'worldbuilding-style-node-hash' })
    run.checkpoint!.approved = { style: '完整复审后已确认的文风内容' }
    await open()
    wrapper.findComponent(DAGCanvas).vm.$emit('nodeDetail', 'worldbuilding_style_deviation'); await flushPromises()
    const detail = wrapper.find('.setup-node-detail')
    expect(detail.text()).toContain('此候选已被完整复审替代')
    expect(detail.text()).toContain('被完整复审替代的候选（历史）')
    await expand('被完整复审替代的候选（历史）')
    await expand('查看被替代的候选与已确认内容')
    await expand('报告对应的内容版本（技术详情）')
    expect(detail.text()).toContain('完整复审后已确认的文风内容')
    expect(detail.text()).toContain('仅对应历史候选，不代表完整复审后的内容')
    expect(detail.find('[aria-label="该单元当前候选"]').exists()).toBe(false)
    expect(detail.text()).toContain('review-commit')
  })
})
