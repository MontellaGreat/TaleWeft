import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NInput, NInputNumber, NMessageProvider, NModal, NProgress, NRadio, NRadioGroup, NSpace, NSwitch, NText } from 'naive-ui'
import AutopilotPanel from '../src/components/autopilot/AutopilotPanel.vue'
import { autopilotApi } from '../src/api/autopilot'
import { aiInvocationApi, type InvocationResponseDTO } from '../src/api/aiInvocation'

const invocationStore = vi.hoisted(() => ({ open: vi.fn() }))
vi.mock('../src/stores/aiInvocationStore', () => ({ useAIInvocationStore: () => invocationStore }))
vi.mock('../src/config/features', () => ({ featureFlags: { aiInvocationDebug: false } }))
vi.mock('../src/api/autopilot', () => ({ autopilotApi: {
  getStatus: vi.fn(), getCommand: vi.fn(), listContinuationProposals: vi.fn(), resume: vi.fn(), start: vi.fn(),
}, getAutopilotErrorDetail: () => '', isAutopilotHttpError: () => false, isAutopilotNotFoundError: () => false }))
vi.mock('../src/api/aiInvocation', () => ({ aiInvocationApi: {
  get: vi.fn(), retry: vi.fn(), resume: vi.fn(), accept: vi.fn(), commit: vi.fn(),
} }))

const stopped = {
  novel_id: 'novel', autopilot_status: 'stopped', current_stage: 'macro_planning',
  target_chapters: 30, completed_chapters: 0, auto_approve_mode: true,
  active_invocation_session_id: 'interrupted-macro', active_invocation_operation: 'autopilot.macro.plan',
  active_invocation_node_key: 'planning-quick-macro', active_invocation_status: 'prompt_compiled',
  active_invocation_policy: 'DIRECT', requires_ai_review: true, has_active_invocation: true,
  invocation_recovery_required: true,
  invocation_recovery_message: '调用中断，旧版缺少调用回执，请检查并恢复。',
}
const compiled = (): InvocationResponseDTO => ({
  session: { id: 'interrupted-macro', operation: 'autopilot.macro.plan', node_key: 'planning-quick-macro',
    policy: 'DIRECT', status: 'prompt_compiled', context: { novel_id: 'novel' }, attempts: [],
    prompt_snapshot: { prompt: { system: '检查这份原始规划请求。', user: '为灯塔守卫生成部卷幕。' } } },
})
let app: App | null = null, host: HTMLDivElement | null = null
async function flush() {
  for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) }
}
function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!found) throw new Error(`Missing button ${label}`)
  return found
}
async function mount(status: Record<string, unknown> = stopped) {
  vi.mocked(autopilotApi.getStatus).mockResolvedValue(status)
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(AutopilotPanel, { novelId: 'novel', renderLivePreview: false }) }) })
  for (const [name, component] of Object.entries({ NAlert, NButton, NForm, NFormItem, NInput, NInputNumber, NModal, NProgress, NRadio, NRadioGroup, NSpace, NSwitch, NText })) app.component(name, component)
  app.mount(host); await flush()
}
beforeEach(() => {
  vi.mocked(autopilotApi.listContinuationProposals).mockResolvedValue([])
  vi.mocked(aiInvocationApi.get).mockResolvedValue(compiled())
})
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })

describe('stopped autopilot invocation recovery', () => {
  it('keeps stopped visible and opens an interrupted legacy request read-only without a debug flag', async () => {
    await mount()
    expect(document.querySelector('.stage-text')?.textContent).toBe('已停止')
    expect(invocationStore.open).not.toHaveBeenCalled()
    expect(aiInvocationApi.get).not.toHaveBeenCalled()
    button('检查并恢复 AI 请求').click(); await flush()
    expect(aiInvocationApi.get).toHaveBeenCalledWith('interrupted-macro')
    expect(document.body.textContent).toContain('旧版缺少调用回执')
    expect(document.body.textContent).toContain('为灯塔守卫生成部卷幕。')
    expect(document.body.textContent).not.toContain('从未请求')
    expect(aiInvocationApi.retry).not.toHaveBeenCalled()
    expect(aiInvocationApi.resume).not.toHaveBeenCalled()
    expect(autopilotApi.start).not.toHaveBeenCalled()
  })

  it('retries only after a click and shows the recorded provider failure without restarting autopilot', async () => {
    vi.mocked(aiInvocationApi.retry).mockResolvedValue({ ...compiled(),
      session: { ...compiled().session, status: 'failed', attempts: ['failure'] },
      attempt: { id: 'failure', session_id: 'interrupted-macro', status: 'failed', content: '', error: '网关 502：上游连接失败' } })
    await mount()
    button('检查并恢复 AI 请求').click(); await flush()
    button('确认重新请求').click(); await flush()
    expect(aiInvocationApi.retry).toHaveBeenCalledOnce()
    expect(aiInvocationApi.retry).toHaveBeenCalledWith('interrupted-macro', { resumed_by: 'user' })
    expect(document.body.textContent).toContain('网关 502：上游连接失败')
    expect(document.querySelector('.stage-text')?.textContent).toBe('已停止')
    expect(autopilotApi.start).not.toHaveBeenCalled()
    expect(autopilotApi.resume).not.toHaveBeenCalled()
  })

  it('requires separate author actions to adopt and persist a recovered result', async () => {
    const result: InvocationResponseDTO = { ...compiled(),
      session: { ...compiled().session, status: 'awaiting_acceptance' },
      attempt: { id: 'result', session_id: 'interrupted-macro', status: 'succeeded', content: '{"parts":[{"title":"归航"}]}' } }
    vi.mocked(aiInvocationApi.get).mockResolvedValue(result)
    vi.mocked(aiInvocationApi.accept).mockResolvedValue({ ...result, session: { ...result.session, status: 'awaiting_commit' },
      decision: { id: 'decision', session_id: 'interrupted-macro', attempt_id: 'result', decision: 'accepted', accept_content: true,
        commit_prompt_version: false, commit_variable_outputs: true, commit_variable_bindings: false } })
    vi.mocked(aiInvocationApi.commit).mockImplementation(async () => {
      vi.mocked(autopilotApi.getStatus).mockResolvedValue({ ...stopped, active_invocation_session_id: '',
        active_invocation_status: 'completed', requires_ai_review: false, has_active_invocation: false })
      return { ...result, session: { ...result.session, status: 'completed' },
        commit: { id: 'commit', session_id: 'interrupted-macro', decision_id: 'decision', status: 'succeeded', steps: [] } }
    })
    await mount({ ...stopped, active_invocation_status: 'awaiting_acceptance' })
    button('检查并恢复 AI 请求').click(); await flush()
    expect(document.body.textContent).toContain('归航')
    expect(aiInvocationApi.accept).not.toHaveBeenCalled()
    button('采纳生成结果').click(); await flush()
    expect(aiInvocationApi.accept).toHaveBeenCalledOnce()
    expect(aiInvocationApi.commit).not.toHaveBeenCalled()
    button('提交已采纳结果').click(); await flush()
    expect(aiInvocationApi.commit).toHaveBeenCalledWith('interrupted-macro', 'decision')
    expect(document.body.textContent).toContain('会话已完成')
    expect(autopilotApi.start).not.toHaveBeenCalled()
    expect(autopilotApi.resume).not.toHaveBeenCalled()
  })

  it('preserves a failed read as an actionable load error without sending another model request', async () => {
    vi.mocked(aiInvocationApi.get).mockRejectedValueOnce(new Error('会话读取失败'))
    await mount()
    button('检查并恢复 AI 请求').click(); await flush()
    expect(document.body.textContent).toContain('会话读取失败')
    button('重试加载会话').click(); await flush()
    expect(aiInvocationApi.get).toHaveBeenCalledTimes(2)
    expect(document.body.textContent).toContain('为灯塔守卫生成部卷幕。')
    expect(aiInvocationApi.retry).not.toHaveBeenCalled()
  })

  it('retries a failed commit with its existing adoption decision without regenerating the result', async () => {
    vi.mocked(aiInvocationApi.get).mockResolvedValue({ ...compiled(), session: { ...compiled().session, status: 'failed' },
      decision: { id: 'adopted', session_id: 'interrupted-macro', attempt_id: 'result', decision: 'accepted', accept_content: true,
        commit_prompt_version: false, commit_variable_outputs: true, commit_variable_bindings: false },
      commit: { id: 'failed-commit', session_id: 'interrupted-macro', decision_id: 'adopted', status: 'failed', steps: [], error: '结构写入失败' } })
    vi.mocked(aiInvocationApi.commit).mockResolvedValue({ ...compiled(), session: { ...compiled().session, status: 'completed' } })
    await mount({ ...stopped, active_invocation_status: 'failed' })
    button('检查并恢复 AI 请求').click(); await flush()
    expect(document.body.textContent).toContain('结构写入失败')
    expect(document.body.textContent).not.toContain('确认重新请求')
    button('提交已采纳结果').click(); await flush()
    expect(aiInvocationApi.commit).toHaveBeenCalledWith('interrupted-macro', 'adopted')
    expect(aiInvocationApi.retry).not.toHaveBeenCalled()
  })

  it('prevents repeated retry clicks while a manual model request is pending', async () => {
    let finish!: (data: InvocationResponseDTO) => void
    vi.mocked(aiInvocationApi.retry).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    await mount()
    button('检查并恢复 AI 请求').click(); await flush()
    button('确认重新请求').click(); button('确认重新请求').click(); await flush()
    expect(aiInvocationApi.retry).toHaveBeenCalledOnce()
    finish({ ...compiled(), session: { ...compiled().session, status: 'failed' } }); await flush()
    expect(autopilotApi.start).not.toHaveBeenCalled()
  })

  it('does not revive a completed invocation from stale active flags', async () => {
    await mount({ ...stopped, active_invocation_status: 'completed' })
    expect(document.body.textContent).not.toContain('检查并恢复 AI 请求')
    expect(document.querySelector('.stage-text')?.textContent).toBe('已停止')
    expect(invocationStore.open).not.toHaveBeenCalled()
  })
})
