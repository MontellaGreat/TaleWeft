import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import StoryStructureTree from '../src/components/StoryStructureTree.vue'

const fixture = vi.hoisted(() => ({
  tree: vi.fn(), status: vi.fn(), progress: vi.fn(), watchers: [] as any[],
  message: { error: vi.fn(), success: vi.fn() },
}))
vi.mock('../src/api/structure', () => ({ structureApi: { getTree: fixture.tree } }))
vi.mock('../src/api/autopilot', () => ({ autopilotApi: { getStatus: fixture.status }, isAutopilotHttpError: () => false }))
vi.mock('../src/api/dataProtection', () => ({ dataProtectionApi: {} }))
vi.mock('../src/api/chapter', () => ({ chapterApi: {} }))
vi.mock('../src/api/planning', () => ({ planningApi: { getMacroProgress: fixture.progress },
  watchMacroPlanProgress: vi.fn((_slug, handlers) => {
    const controller = new AbortController()
    fixture.watchers.push({ handlers, controller })
    return controller
  }),
}))
vi.mock('../src/config/performance', () => ({ runtimePerformance: { network: { shortTaskTimeoutMs: 30000 }, workbench: {
  storyTreeMacroProgressPollMs: 100, storyTreeMacroPersistRetryAttempts: 2, storyTreeMacroPersistRetryDelayMs: 10,
} } }))
vi.mock('naive-ui', () => ({ NTree: { name: 'NTree' }, NEmpty: { name: 'NEmpty' }, NSpin: { name: 'NSpin' },
  NTag: { name: 'NTag' }, NSpace: { name: 'NSpace' }, NDropdown: { name: 'NDropdown' },
  NModal: { name: 'NModal' }, NInput: { name: 'NInput' },
  useMessage: () => fixture.message, useDialog: () => ({}),
}))
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const Button = defineComponent({ emits: ['click'], setup(_, { slots, emit }) {
  return () => h('button', { onClick: () => emit('click') }, slots.default?.())
} })
const Tree = defineComponent({ props: ['data'], setup(props) {
  const labels = (nodes: any[]): string => nodes.map(node => `${node.label} ${labels(node.children || [])}`).join(' ')
  return () => h('div', { 'data-testid': 'tree' }, labels(props.data || []))
} })
const stubs = { NTree: Tree, NEmpty: defineComponent({ props: ['description'], setup(props, { slots }) {
  return () => h('div', [props.description, slots.extra?.()])
} }), NSpin: defineComponent({ setup: () => () => h('span', { 'data-testid': 'spinner' }) }),
  NTag: Box, NSpace: Box, NButton: Button, NAlert: Box, NDropdown: true, NModal: true, NInput: true }
let wrapper: VueWrapper | undefined
const running = () => ({ autopilot_status: 'running', current_stage: 'macro_planning', writing_substep: 'macro_planning' })
const mountTree = () => { wrapper = mount(StoryStructureTree, { props: { slug: 'book' }, global: { stubs } }); return wrapper }
beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  fixture.watchers.length = 0
  fixture.tree.mockResolvedValue({ tree: [] })
  fixture.status.mockResolvedValue(running())
  fixture.progress.mockResolvedValue({ data: { status: 'idle', message: '' } })
})
afterEach(() => { wrapper?.unmount(); vi.useRealTimers() })

it('reconciles idle observation progress with durable review state, even when stopped and macro substep remains', async () => {
  const page = mountTree()
  await flushPromises()
  expect(page.text()).toContain('正在生成叙事结构')
  fixture.status.mockResolvedValue({ ...running(), autopilot_status: 'stopped', current_stage: 'paused_for_review',
    needs_review: true, requires_ai_review: true, active_invocation_session_id: 'session', active_invocation_status: 'prompt_compiled' })
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(page.text()).not.toContain('正在生成叙事结构')
  expect(page.text()).toMatch(/等待.*处理|待审阅/)
  expect(page.find('[aria-label="等待结构节点"]').exists()).toBe(false)
  const action = page.findAll('button').find(button => button.text().includes('运行中心'))!
  expect(action).toBeDefined()
  await action.trigger('click')
  expect(page.emitted('openRuntime')).toHaveLength(1)
})

it('shows a durable failure even if the in-memory progress endpoint remains idle', async () => {
  const page = mountTree()
  await flushPromises()
  fixture.status.mockResolvedValue({ ...running(), autopilot_status: 'error', pipeline_error: '网关 502，等待重试' })
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(page.text()).toContain('网关 502')
  expect(page.find('[aria-label="等待结构节点"]').exists()).toBe(false)
})

it('continues recovery polling after the observation stream times out, without cancelling generation', async () => {
  mountTree()
  await flushPromises()
  fixture.watchers[0].handlers.onTerminal({ status: 'timeout', message: '宏观规划观摩流超时' })
  const previous = fixture.progress.mock.calls.length
  await vi.advanceTimersByTimeAsync(200)
  await flushPromises()
  expect(fixture.progress.mock.calls.length).toBeGreaterThan(previous)
  expect(wrapper!.text()).toMatch(/轮询|重新查询/)
  expect(wrapper!.text()).not.toContain('规划失败')
})

it('preserves preview nodes and reports missing persistence after the generator completes without a saved tree', async () => {
  const page = mountTree()
  await flushPromises()
  const handlers = fixture.watchers[0].handlers
  handlers.onNode({ type: 'part', part_index: 0, title: '保留的第一部预览' })
  handlers.onTerminal({ status: 'completed' })
  await vi.advanceTimersByTimeAsync(50)
  await flushPromises()
  expect(page.text()).toContain('保留的第一部预览')
  expect(page.text()).toMatch(/尚未.*写入|尚未.*正式结构/)
  expect(page.find('[aria-label="下一批节点生成中"]').exists()).toBe(false)
})

it('ignores late stream callbacks belonging to the previous book', async () => {
  const page = mountTree()
  await flushPromises()
  const previous = fixture.watchers[0].handlers
  fixture.status.mockResolvedValue({ autopilot_status: 'stopped', current_stage: 'idle' })
  await page.setProps({ slug: 'other-book' })
  await flushPromises()
  previous.onNode({ type: 'part', part_index: 0, title: '旧书迟到结构' })
  previous.onTerminal({ status: 'failed', message: '旧书迟到错误' })
  await flushPromises()
  expect(page.text()).not.toContain('旧书迟到')
  expect(fixture.tree).toHaveBeenCalledWith('other-book')
})

it('opens an existing compiled request as waiting for recovery without claiming no provider request was made', async () => {
  fixture.status.mockResolvedValue({ ...running(), autopilot_status: 'stopped', current_stage: 'paused_for_review',
    active_invocation_session_id: 'legacy-session', active_invocation_status: 'prompt_compiled' })
  const page = mountTree()
  await flushPromises()
  expect(page.text()).toContain('等待 AI 请求处理')
  expect(page.text()).toContain('旧版缺少调用回执')
  expect(page.text()).not.toMatch(/没有发出|尚未调用/)
  expect(page.find('[aria-label="等待结构节点"]').exists()).toBe(false)
})

it('accepts the durable progress endpoint review terminal while retaining a retryable observation path', async () => {
  mountTree()
  await flushPromises()
  fixture.status.mockRejectedValue(new Error('status connection unavailable'))
  fixture.progress.mockResolvedValue({ data: { status: 'awaiting_review', message: '生成已中断，请处理当前请求' } })
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(wrapper!.text()).toContain('生成已中断')
  expect(wrapper!.find('[aria-label="等待结构节点"]').exists()).toBe(false)
  const previous = fixture.progress.mock.calls.length
  await vi.advanceTimersByTimeAsync(100)
  expect(fixture.progress.mock.calls.length).toBeGreaterThan(previous)
})

it('reports unavailable observation honestly and can still discover a later saved tree', async () => {
  mountTree()
  await flushPromises()
  fixture.status.mockRejectedValue(new Error('status unavailable'))
  fixture.progress.mockRejectedValue(new Error('progress unavailable'))
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(wrapper!.text()).toContain('任务是否完成尚未确认')
  fixture.status.mockResolvedValue(running())
  fixture.progress.mockResolvedValue({ data: { status: 'completed', message: '规划完成' } })
  fixture.tree.mockResolvedValue({ tree: [{ id: 'part-saved', node_type: 'part', title: '正式结构', children: [] }] })
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(wrapper!.text()).toContain('正式结构')
  expect(wrapper!.find('[aria-label="等待结构节点"]').exists()).toBe(false)
})

it('restores genuine saved preview nodes for an interrupted invocation after reopening', async () => {
  fixture.status.mockResolvedValue({ ...running(), autopilot_status: 'stopped', current_stage: 'paused_for_review',
    active_invocation_session_id: 'legacy-session', active_invocation_status: 'prompt_compiled' })
  fixture.progress.mockResolvedValue({ data: { status: 'interrupted', invocation_session_id: 'legacy-session',
    message: '上次调用已中断，旧版缺少调用回执，请在运行中心处理。',
    preview_parts: [{ title: '已收到的部', volumes: [{ title: '已收到的卷', acts: [{ title: '已收到的幕' }] }] }] } })
  const page = mountTree()
  await flushPromises()
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(page.text()).toContain('已收到的幕')
  expect(page.text()).toContain('上次调用已中断')
  expect(page.find('[aria-label="下一批节点生成中"]').exists()).toBe(false)
})

it('keeps a genuinely running request active without a time-based generation cancellation', async () => {
  fixture.progress.mockResolvedValue({ data: { status: 'running', message: '模型请求仍在执行' } })
  const page = mountTree()
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  await flushPromises()
  expect(page.text()).toContain('正在生成叙事结构')
  expect(page.text()).toContain('模型请求仍在执行')
  expect(page.find('[aria-label="等待结构节点"]').exists()).toBe(true)
})

it('loads a confirmed persisted tree while the durable autopilot stage is still awaiting review', async () => {
  fixture.status.mockResolvedValue({ ...running(), current_stage: 'paused_for_review', needs_review: true })
  const page = mountTree()
  await flushPromises()
  fixture.progress.mockResolvedValue({ data: { status: 'completed', message: '结构已写入' } })
  fixture.tree.mockResolvedValue({ tree: [{ id: 'confirmed-part', node_type: 'part', title: '已确认的正式结构', children: [] }] })
  await vi.advanceTimersByTimeAsync(100)
  await flushPromises()
  expect(page.text()).toContain('已确认的正式结构')
  expect(page.text()).not.toContain('尚未读取到正式结构')
})
