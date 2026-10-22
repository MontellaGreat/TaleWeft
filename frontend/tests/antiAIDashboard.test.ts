import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { NMessageProvider } from 'naive-ui'
import AntiAIDashboard from '../src/components/workbench/promptPlaza/AntiAIDashboard.vue'
import * as api from '../src/api/anti-ai'
import { novelApi } from '../src/api/novel'

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { slug: 'novel' } }) }))
vi.mock('../src/api/novel', () => ({ novelApi: { listNovels: vi.fn() } }))
vi.mock('../src/components/workbench/promptPlaza/AntiAIRewriteReview.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/api/anti-ai', () => ({ scanChapter: vi.fn(), getAntiAIStats: vi.fn(), getRules: vi.fn(),
  getAllowlistScenes: vi.fn(), getAuditRuns: vi.fn(), getAuditHistory: vi.fn(), getAuditTrend: vi.fn(), getStreamRuns: vi.fn(), getPromptRuns: vi.fn(), getAuditProgress: vi.fn() }))

let app: App | null = null
let host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
async function mount() {
  vi.mocked(novelApi.listNovels).mockResolvedValue([{ id: 'novel', title: 'Test' }] as never)
  vi.mocked(api.getRules).mockResolvedValue([])
  vi.mocked(api.getAllowlistScenes).mockResolvedValue([])
  vi.mocked(api.getAuditRuns).mockResolvedValue([])
  vi.mocked(api.getAuditHistory).mockResolvedValue([])
  vi.mocked(api.getAuditTrend).mockResolvedValue([])
  vi.mocked(api.getStreamRuns).mockResolvedValue([])
  vi.mocked(api.getPromptRuns).mockResolvedValue([])
  vi.mocked(api.getAuditProgress).mockResolvedValue([])
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(AntiAIDashboard) }) })
  app.mount(host); await flush()
}
function tab(label: string) {
  const item = [...document.querySelectorAll<HTMLElement>('.sub-tab')].find(element => element.textContent?.trim() === label)
  if (!item) throw new Error(`missing tab ${label}`)
  item.click()
}
afterEach(() => { app?.unmount(); host?.remove(); document.body.innerHTML = ''; vi.clearAllMocks() })

describe('Anti-AI execution evidence dashboard', () => {
  it.each([['scanning','正在扫描','running','not_requested'],['persisting','正在归档','completed','not_requested'],
    ['completed','扫描及归档完成','completed','committed'],['failed','扫描或归档失败','completed','failed']])
    ('shows durable independent scanner stage %s', async (stage,label,scanStatus,persistenceStatus) => {
      vi.mocked(api.getAntiAIStats).mockRejectedValue(new Error('offline'))
      await mount()
      vi.mocked(api.getAuditProgress).mockResolvedValue([{run_id:'scan-run',chapter_number:2,stage,scan_status:scanStatus,
        persistence_status:persistenceStatus,content_hash:'hash',rule_version:'rules',job_id:'scan-job',chapter_revision:3}] as never)
      tab('自动审计记录'); await flush()
      document.querySelectorAll<HTMLButtonElement>('button').forEach(button => { if (button.textContent?.includes('刷新记录')) button.click() })
      await flush()
      expect(document.body.textContent).toContain(label)
      expect(document.body.textContent).toContain('scan-run')
      expect(document.body.textContent).toContain('scan-job')
      expect(document.body.textContent).not.toContain('尚未运行自动审计')
    })

  it('shows unavailable progress without presenting a clean no-run state', async () => {
    vi.mocked(api.getAntiAIStats).mockRejectedValue(new Error('offline'))
    await mount()
    vi.mocked(api.getAuditProgress).mockRejectedValue(new Error('progress store offline'))
    tab('自动审计记录'); await flush()
    document.querySelectorAll<HTMLButtonElement>('button').forEach(button => { if (button.textContent?.includes('刷新记录')) button.click() })
    await flush()
    expect(document.body.textContent).toContain('无法确认本次审计终态')
    expect(document.body.textContent).not.toContain('尚未运行自动审计')
  })

  it('labels observed as historical evidence and unconfirmed hooks separately', async () => {
    vi.mocked(api.getAntiAIStats).mockResolvedValue({ total_prompts: 7, anti_ai_prompts: 5, categories_count: 1, cliche_patterns: 35,
      observed_audit_runs: 2, failed_audit_runs: 1, layers: {} as never,
      layer_states: { L1_positive_framing: { implemented: true, configured: true, connected: false, observed: false, failed: false, status: 'configured', connection_evidence: 'provider_hook_not_reported' },
        L7_audit: { implemented: true, configured: true, connected: true, observed: true, failed: false, status: 'observed' } } })
    await mount()
    expect(api.getAntiAIStats).toHaveBeenLastCalledWith('novel')
    expect(document.body.textContent).toContain('已有运行记录')
    expect(document.body.textContent).toContain('运行接线未确认')
    expect(document.body.textContent).not.toContain('已观测到本次运行')
  })

  it('shows unavailable stats instead of keeping stale connected success', async () => {
    vi.mocked(api.getAntiAIStats).mockRejectedValue(new Error('offline'))
    await mount()
    expect(document.body.textContent).toContain('能力状态暂不可用')
    expect(document.querySelectorAll('.layer-card.is-active')).toHaveLength(0)
  })

  it('distinguishes not-run records from record-loading failures', async () => {
    vi.mocked(api.getAntiAIStats).mockRejectedValue(new Error('offline'))
    await mount(); tab('自动审计记录'); await flush()
    expect(document.body.textContent).toContain('尚未运行自动审计')
    vi.mocked(api.getAuditRuns).mockRejectedValue(new Error('archive offline'))
    document.querySelectorAll<HTMLButtonElement>('button').forEach(button => { if (button.textContent?.includes('刷新记录')) button.click() })
    await flush()
    expect(document.body.textContent).toContain('部分审计记录加载失败')
    expect(document.body.textContent).not.toContain('尚未运行自动审计')
  })

  it('labels an older trend score when the newest scan failed', async () => {
    vi.mocked(api.getAntiAIStats).mockRejectedValue(new Error('offline'))
    await mount()
    vi.mocked(api.getAuditTrend).mockResolvedValue([{ chapter_number: 3, severity_score: 1, total_hits: 2, critical_hits: 0,
      hit_density: 1, overall_assessment: '轻微', legacy: false, latest_scan_status: 'failed', latest_persistence_status: 'committed' }])
    tab('自动审计记录'); await flush()
    document.querySelectorAll<HTMLButtonElement>('button').forEach(button => { if (button.textContent?.includes('刷新记录')) button.click() })
    await flush()
    expect(document.body.textContent).toContain('最近扫描或归档未完成；显示上次摘要')
  })
})
