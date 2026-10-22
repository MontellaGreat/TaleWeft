import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import { NMessageProvider } from 'naive-ui'
import ContinuationStructureReview from '../src/components/autopilot/ContinuationStructureReview.vue'
import { autopilotApi, type ContinuationProposal } from '../src/api/autopilot'

vi.mock('../src/api/autopilot', () => ({
  autopilotApi: { confirmContinuationStructure: vi.fn(), rejectContinuationProposal: vi.fn() },
  getAutopilotErrorDetail: () => '',
}))
const proposal = { proposal_id: 'continuation', novel_id: 'novel', mode: 'continue', base_stage: 'completed',
  base_target_chapters: 1, proposed_target_chapters: 3, completed_chapters: 1, from_chapter: 2, rationale: '',
  status: 'awaiting_structure_review', created_at: 'today', run_epoch: 5, structure_change_id: 'change', plan: {},
  structure: [{ title: '后续部', description: '主角寻找失踪线索', volumes: [{ title: '线索卷', acts: [{ title: '追踪幕', description: '调查旧码头' }] }] }],
  structure_review: { proposal_id: 'change', novel_id: 'novel', operation: 'macro_merge', base_hash: 'base', proposal_hash: 'hash',
    snapshot_id: 'snapshot', run_epoch: 5, current_run_epoch: 5, status: 'pending', expired: false, created_at: 'today',
    proposal: { creates: [{ title: '后续部' }], updates: [], deletes: [], affected_chapters: [{ id: 'chapter', number: 1, has_content: true, revision: 2 }] }, audits: [] },
} satisfies ContinuationProposal
let app: App | null = null, host: HTMLDivElement | null = null
const changed = vi.fn(), refresh = vi.fn()
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
function click(label: string) {
  const node = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!node) throw new Error(`Missing button ${label}`)
  node.click()
}
async function mount(item = ref<ContinuationProposal>(proposal)) {
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(ContinuationStructureReview, { novelId: item.value.novel_id, proposal: item.value, onChanged: changed, onRefresh: refresh }) }) })
  app.mount(host); await flush()
}
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })
describe('continuation author structure gate', () => {
  it('previews actual hierarchy and submits only the displayed immutable review after a second confirmation', async () => {
    vi.mocked(autopilotApi.confirmContinuationStructure).mockResolvedValue({ status: 'applied', proposal_id: 'change', snapshot_id: 'snapshot', audit_id: 'audit', state_hash: 'after' })
    await mount()
    for (const text of ['后续部', '线索卷', '追踪幕', '调查旧码头', '新增 1 个节点', '有正文 1 个']) expect(document.body.textContent).toContain(text)
    click('确认采用续写结构'); await flush()
    expect(autopilotApi.confirmContinuationStructure).not.toHaveBeenCalled()
    click('核验版本并应用结构'); await flush()
    expect(autopilotApi.confirmContinuationStructure).toHaveBeenCalledWith('novel', expect.objectContaining({ proposal_id: 'continuation', structure_review: expect.objectContaining({ base_hash: 'base', proposal_hash: 'hash', run_epoch: 5 }) }))
    expect(changed).toHaveBeenCalledOnce()
  })
  it.each(['expired', 'epoch'])('blocks %s reviews while preserving the hierarchy and rejection action', async reason => {
    const review = { ...proposal.structure_review, expired: reason === 'expired', current_run_epoch: reason === 'epoch' ? 6 : 5 }
    await mount(ref({ ...proposal, structure_review: review }))
    expect(document.body.textContent).toContain('追踪幕')
    expect(document.body.textContent).toContain('结构审阅已过期或版本已变化')
    expect(document.body.textContent).not.toContain('确认采用续写结构')
    expect(document.body.textContent).toContain('放弃此次续写')
  })
  it('keeps the preview on writer conflict and requests a refresh without announcing success', async () => {
    vi.mocked(autopilotApi.confirmContinuationStructure).mockRejectedValue(new Error('正文已被作者编辑'))
    await mount(); click('确认采用续写结构'); await flush(); click('核验版本并应用结构'); await flush()
    expect(document.body.textContent).toContain('正文已被作者编辑')
    expect(document.body.textContent).toContain('追踪幕')
    expect(changed).not.toHaveBeenCalled(); expect(refresh).toHaveBeenCalledOnce()
  })
  it('requires explicit rejection and does not confirm structure while still planning', async () => {
    vi.mocked(autopilotApi.rejectContinuationProposal).mockResolvedValue({ ...proposal, status: 'rejected' })
    await mount(ref({ ...proposal, status: 'macro_planning' }))
    expect(document.body.textContent).not.toContain('确认采用续写结构')
    click('放弃此次续写'); await flush()
    expect(autopilotApi.rejectContinuationProposal).not.toHaveBeenCalled()
    click('确定放弃续写'); await flush()
    expect(autopilotApi.rejectContinuationProposal).toHaveBeenCalledWith('novel', 'continuation')
    expect(changed).toHaveBeenCalledOnce()
  })
  it('closes an old confirmation after the selected novel changes', async () => {
    const selected = ref<ContinuationProposal>(proposal)
    await mount(selected); click('确认采用续写结构'); await flush()
    selected.value = { ...proposal, novel_id: 'other', proposal_id: 'other-continuation' }; await flush()
    expect(document.body.textContent).not.toContain('核验版本并应用结构')
    expect(autopilotApi.confirmContinuationStructure).not.toHaveBeenCalled()
  })
  it('rejects a changed review hash while its previous confirmation is open', async () => {
    const selected = ref<ContinuationProposal>(proposal)
    await mount(selected); click('确认采用续写结构'); await flush()
    selected.value = { ...proposal, structure_review: { ...proposal.structure_review, proposal_hash: 'changed' } }; await flush()
    expect(document.body.textContent).not.toContain('核验版本并应用结构')
    expect(autopilotApi.confirmContinuationStructure).not.toHaveBeenCalled()
  })
})
