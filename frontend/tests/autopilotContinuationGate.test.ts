import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NInput, NInputNumber, NMessageProvider, NModal, NProgress, NRadio, NRadioGroup, NSpace, NSwitch, NText } from 'naive-ui'
import AutopilotPanel from '../src/components/autopilot/AutopilotPanel.vue'
import { autopilotApi, type ContinuationProposal } from '../src/api/autopilot'

vi.mock('../src/api/autopilot', () => ({ autopilotApi: {
  getStatus: vi.fn(), getCommand: vi.fn(), listContinuationProposals: vi.fn(), resume: vi.fn(), start: vi.fn(),
  confirmContinuationStructure: vi.fn(), rejectContinuationProposal: vi.fn(),
}, getAutopilotErrorDetail: () => '', isAutopilotHttpError: () => false, isAutopilotNotFoundError: () => false }))
vi.mock('../src/stores/aiInvocationStore', () => ({ useAIInvocationStore: () => ({}) }))
vi.mock('../src/config/features', () => ({ featureFlags: { aiInvocationDebug: false } }))
const status = { novel_id: 'novel', current_stage: 'paused_for_review', autopilot_status: 'running', auto_approve_mode: true,
  target_chapters: 3, completed_chapters: 1, needs_review: true, review_gate: { status: 'ready', can_resume: true } }
const proposal = { proposal_id: 'continuation', novel_id: 'novel', mode: 'continue', base_stage: 'completed',
  base_target_chapters: 1, proposed_target_chapters: 3, completed_chapters: 1, from_chapter: 2, rationale: '',
  status: 'awaiting_structure_review', created_at: 'today', run_epoch: 5, structure_change_id: 'change', plan: {},
  structure: [{ title: '续写部', volumes: [{ title: '新卷', acts: [{ title: '新幕' }] }] }],
  structure_review: { proposal_id: 'change', novel_id: 'novel', operation: 'macro_merge', base_hash: 'base', proposal_hash: 'hash',
    snapshot_id: 'snapshot', run_epoch: 5, current_run_epoch: 5, status: 'pending', expired: false, created_at: 'today',
    proposal: { creates: [], updates: [], deletes: [], affected_chapters: [] }, audits: [] },
} satisfies ContinuationProposal
let app: App | null = null, host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 6; i++) { await nextTick(); if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(0); else await new Promise(resolve => setTimeout(resolve, 0)) } }
function click(label: string) {
  const node = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!node) throw new Error(`Missing button ${label}`)
  node.click()
}
async function mount(initialStatus = status) {
  vi.mocked(autopilotApi.getStatus).mockResolvedValue(initialStatus)
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(AutopilotPanel, { novelId: 'novel', renderLivePreview: false }) }) })
  for (const [name, component] of Object.entries({ NAlert, NButton, NForm, NFormItem, NInput, NInputNumber, NModal, NProgress, NRadio, NRadioGroup, NSpace, NSwitch, NText })) app.component(name, component)
  app.mount(host); await flush()
}
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.useRealTimers(); vi.clearAllMocks() })
describe('autopilot continuation gate integration', () => {
  it('restores a pending start after reopening and explains why the executor has not acknowledged', async () => {
    vi.useFakeTimers()
    vi.mocked(autopilotApi.listContinuationProposals).mockResolvedValue([])
    await mount({ ...status, current_stage: 'planning', autopilot_status: 'stopped', needs_review: false,
      command_id: 'pending-start', command_type: 'start', command_status: 'pending', execution_confirmed: false,
      control_message: '请求已保存，等待执行器确认；执行器暂无有效心跳，请检查后台状态',
    } as typeof status)
    expect(document.querySelector('.ap-inline-alert')?.textContent).toContain('执行器暂无有效心跳')
    expect(autopilotApi.start).not.toHaveBeenCalled()
    vi.mocked(autopilotApi.getStatus).mockResolvedValue({ ...status, current_stage: 'planning', autopilot_status: 'stopped', needs_review: false,
      command_id: 'pending-start', command_type: 'start', command_status: 'pending', execution_confirmed: false,
      execution_reservation_expired: true, control_message: '启动请求的执行租约已过期，尚未启动；请重新提交启动请求',
    })
    await vi.advanceTimersByTimeAsync(30000)
    expect(document.querySelector('.ap-inline-alert')?.textContent).toContain('请重新提交启动请求')
    expect(document.body.textContent).not.toContain('执行器已确认启动')
    vi.mocked(autopilotApi.start).mockResolvedValue(new Response(JSON.stringify({ accepted: true,
      command_id: 'retried-start', command_status: 'pending', execution_confirmed: false })))
    click('🚀 启动全托管'); await flush()
    click('启动'); await flush()
    expect(autopilotApi.start).toHaveBeenCalledOnce()
  })
  it('restores the pending structure after reopening and replaces generic resume with explicit author confirmation', async () => {
    vi.mocked(autopilotApi.listContinuationProposals).mockResolvedValue([proposal])
    vi.mocked(autopilotApi.confirmContinuationStructure).mockResolvedValue({ status: 'applied', proposal_id: 'change', snapshot_id: 'snapshot', audit_id: 'audit', state_hash: 'after' })
    await mount()
    expect(autopilotApi.listContinuationProposals).toHaveBeenCalledWith('novel')
    expect(document.body.textContent).toContain('续写部')
    expect(document.body.textContent).toContain('确认采用续写结构')
    expect(document.body.textContent).not.toContain('再次确认 · 继续')
    click('确认采用续写结构'); await flush()
    vi.mocked(autopilotApi.listContinuationProposals).mockResolvedValue([{ ...proposal, status: 'applied' }])
    vi.mocked(autopilotApi.getStatus).mockResolvedValue({ ...status, current_stage: 'act_planning', autopilot_status: 'stopped', needs_review: false })
    click('核验版本并应用结构'); await flush()
    expect(autopilotApi.confirmContinuationStructure).toHaveBeenCalledOnce()
    expect(autopilotApi.resume).not.toHaveBeenCalled()
    expect(autopilotApi.start).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain('待作者审阅续写结构')
  })
  it('holds generic resume while proposal loading is incomplete', async () => {
    let finish!: (data: ContinuationProposal[]) => void
    vi.mocked(autopilotApi.listContinuationProposals).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    await mount()
    expect(document.body.textContent).not.toContain('再次确认 · 继续')
    finish([proposal]); await flush()
    expect(document.body.textContent).toContain('确认采用续写结构')
    expect(autopilotApi.resume).not.toHaveBeenCalled()
  })
})
