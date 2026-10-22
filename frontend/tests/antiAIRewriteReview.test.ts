import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import { NMessageProvider } from 'naive-ui'
import AntiAIRewriteReview from '../src/components/workbench/promptPlaza/AntiAIRewriteReview.vue'
import type { RewriteCandidate } from '../src/types/anti-ai'
import * as api from '../src/api/anti-ai'

vi.mock('../src/api/anti-ai', () => ({
  getRewritePolicy: vi.fn(), saveRewritePolicy: vi.fn(), getRewriteCandidates: vi.fn(),
  confirmRewriteCandidate: vi.fn(), rejectRewriteCandidate: vi.fn(), regenerateRewriteCandidate: vi.fn(),
}))

const candidate = { candidate_id: 'candidate', novel_id: 'novel', run_id: 'run', chapter_number: 1, kind: 'rewrite', attempt: 1,
  base_hash: 'base-hash', base_revision: 4, base_content: '原稿', content: '候选正文', content_hash: 'candidate-hash',
  diff: '-原稿\n+候选正文', status: 'pending_review', created_at: 'today', stale: false, is_generating: false,
  can_confirm: true, run_epoch: 5, novel_stage: 'paused_for_review', current_revision: 4, current_content_hash: 'base-hash',
  audit: { accepted: true, scan: { scan_status: 'completed', metrics: { severity_score: 0, critical_hits: 0 } },
    quality: { status: 'passed', passed: true }, voice: { status: 'completed', similarity_score: .9 } },
} satisfies RewriteCandidate
let app: App | null = null
let host: HTMLDivElement | null = null

async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
function click(label: string) {
  const button = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!button) throw new Error(`Button missing: ${label}`)
  button.click()
}
async function mount(novelId = ref<string | null>('novel')) {
  vi.mocked(api.getRewritePolicy).mockResolvedValue({ mode: 'review', max_attempts: 2, max_severity: 3 })
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(AntiAIRewriteReview, { novelId: novelId.value }) }) })
  app.mount(host)
  await flush()
}
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })

describe('author candidate UI', () => {
  it('does not call an existing failed or mismatched final audit archived', async () => {
    vi.mocked(api.getRewriteCandidates).mockResolvedValue([{ ...candidate, status: 'committed', can_confirm: false,
      audit: { ...candidate.audit, committed_revision: 5, final_audit: { scan_status: 'failed', persistence_status: 'committed', content_hash: 'other-body', run_id: 'audit' } } }])
    await mount()
    expect(document.body.textContent).toContain('最终审计待核验')
    expect(document.body.textContent).toContain('重新核验最终审计')
    expect(document.body.textContent).not.toContain('已提交并归档')
  })

  it('does not announce archive success for an incomplete confirm response', async () => {
    vi.mocked(api.getRewriteCandidates).mockResolvedValue([candidate])
    vi.mocked(api.confirmRewriteCandidate).mockResolvedValue({ status: 'committed', candidate: { ...candidate, status: 'committed' }, requires_resume: true })
    await mount()
    click('确认采用此候选'); await flush()
    click('复验并提交'); await flush()
    expect(document.body.textContent).toContain('提交或最终审计尚未确认完成')
    expect(document.body.textContent).not.toContain('正文已提交，最终审计已归档')
  })
  it('offers retry when derivation failed even if narrative sync was successful', async () => {
    vi.mocked(api.getRewriteCandidates).mockResolvedValue([{ ...candidate, status: 'committed', can_confirm: false,
      audit: { ...candidate.audit, committed_revision: 5, final_audit: { scan_status: 'completed', persistence_status: 'committed', content_hash: 'candidate-hash', run_id: 'audit' }, aftermath: { narrative_sync_ok: true, derivation_status: 'failed', error: '派生失败' } } }])
    await mount()
    expect(document.body.textContent).toContain('章后派生待重试')
    expect(document.body.textContent).toContain('重新核验最终审计')
    expect(document.body.textContent).not.toContain('已提交并归档')
  })
  it('requires the confirmation dialog and sends the displayed immutable versions', async () => {
    vi.mocked(api.getRewriteCandidates).mockResolvedValue([candidate])
    vi.mocked(api.confirmRewriteCandidate).mockResolvedValue({ status: 'committed', candidate: { ...candidate, status: 'committed' }, requires_resume: true })
    await mount()
    expect(document.body.textContent).toContain('-原稿')
    click('确认采用此候选'); await flush()
    expect(api.confirmRewriteCandidate).not.toHaveBeenCalled()
    click('复验并提交'); await flush()
    expect(api.confirmRewriteCandidate).toHaveBeenCalledWith('novel', 'candidate', { expected_candidate_hash: 'candidate-hash', expected_base_hash: 'base-hash', expected_base_revision: 4, expected_run_epoch: 5 })
  })

  it('shows expired candidate differences and offers current-body regeneration without confirmation', async () => {
    vi.mocked(api.getRewriteCandidates).mockResolvedValue([{ ...candidate, stale: true, can_confirm: false, current_revision: 6, current_content_hash: 'current' }])
    vi.mocked(api.regenerateRewriteCandidate).mockResolvedValue({ status: 'awaiting_confirmation', candidate })
    await mount()
    expect(document.body.textContent).toContain('版本已过期')
    expect(document.body.textContent).not.toContain('确认采用此候选')
    click('根据当前正文重新生成'); await flush()
    expect(api.regenerateRewriteCandidate).toHaveBeenCalledWith('novel', expect.objectContaining({ current_revision: 6, current_content_hash: 'current' }))
  })

  it('ignores a slow response after changing the selected novel', async () => {
    let finish!: (items: RewriteCandidate[]) => void
    vi.mocked(api.getRewriteCandidates).mockImplementation(id => id === 'novel' ? new Promise(resolve => { finish = resolve }) : Promise.resolve([{ ...candidate, novel_id: 'other', chapter_number: 9 }]))
    const selection = ref<string | null>('novel')
    await mount(selection)
    selection.value = 'other'; await flush()
    finish([candidate]); await flush()
    expect(document.body.textContent).toContain('第 9 章')
    expect(document.body.textContent).not.toContain('第 1 章')
  })
})
