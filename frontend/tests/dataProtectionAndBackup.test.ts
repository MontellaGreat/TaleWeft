import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App, type Component } from 'vue'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import DataChangeHistory from '../src/components/workbench/DataChangeHistory.vue'
import DatabaseBackupSection from '../src/components/settings/sections/DatabaseBackupSection.vue'
import { dataProtectionApi, type DataChangeProposal } from '../src/api/dataProtection'
import { databaseBackupApi } from '../src/api/databaseBackup'

vi.mock('../src/api/dataProtection', () => ({ dataProtectionApi: { list: vi.fn(), rollback: vi.fn() } }))
vi.mock('../src/api/databaseBackup', () => ({ databaseBackupApi: { list: vi.fn(), create: vi.fn(), verify: vi.fn(), restoreCopy: vi.fn() } }))
const proposal = { proposal_id: 'change', novel_id: 'novel', operation: 'delete_nodes', base_hash: 'before', proposal_hash: 'proposal',
  after_hash: 'after', snapshot_id: 'protected', run_epoch: 1, current_run_epoch: 2, status: 'applied', expired: false, created_at: 'today',
  proposal: { affected_chapters: [{ id: 'chapter', number: 1, has_content: true, revision: 3 }] }, audits: [{ audit_id: 'audit', event: 'applied', created_at: 'today' }],
} satisfies DataChangeProposal
const backup = { backup_id: 'backup', directory: 'project/backups/backup', status: 'verification_required', created_at: 'today',
  database: { filename: 'plotpilot.db', size: 100, sha256: 'hash' }, validation: { counts: { novels: 1, chapters: 2 }, integrity_check: 'ok', foreign_key_check: 'ok', schema_hash: 'schema', user_version: 0 } }
let app: App | null = null
let host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
function click(label: string) { const node = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label); if (!node) throw new Error(`Missing button ${label}`); node.click() }
async function mount(component: Component, props: () => object = () => ({})) {
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(NDialogProvider, {}, { default: () => h(component, props()) }) }) })
  app.mount(host); await flush()
}
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })
describe('data protection and backup author actions', () => {
  it('requires explicit restore confirmation and preserves the displayed proposal', async () => {
    vi.mocked(dataProtectionApi.list).mockResolvedValue([proposal])
    vi.mocked(dataProtectionApi.rollback).mockResolvedValue({ status: 'rolled_back', proposal_id: 'change', snapshot_id: 'protected', audit_id: 'restore-audit', state_hash: 'restored' })
    await mount(DataChangeHistory, () => ({ novelId: 'novel' }))
    expect(document.body.textContent).toContain('保护快照 protected')
    click('恢复此次变更前的数据'); await flush()
    expect(dataProtectionApi.rollback).not.toHaveBeenCalled()
    click('检查并恢复'); await flush()
    expect(dataProtectionApi.rollback).toHaveBeenCalledWith('novel', proposal)
  })
  it('rejects an old dialog after switching novels', async () => {
    vi.mocked(dataProtectionApi.list).mockResolvedValue([proposal])
    const novelId = ref('novel')
    await mount(DataChangeHistory, () => ({ novelId: novelId.value }))
    click('恢复此次变更前的数据'); await flush()
    novelId.value = 'other'; await flush()
    click('检查并恢复'); await flush()
    expect(dataProtectionApi.rollback).not.toHaveBeenCalled()
  })
  it('shows verified backup status and the independent recovery receipt', async () => {
    vi.mocked(databaseBackupApi.list).mockResolvedValue([backup] as never)
    vi.mocked(databaseBackupApi.create).mockResolvedValue({ ...backup, status: 'verified' } as never)
    vi.mocked(databaseBackupApi.restoreCopy).mockResolvedValue({ backup_id: 'backup', status: 'restored_verified', restored_database: 'project/backups/restored/copy.db', active_database_changed: false, sha256: 'hash', validation: backup.validation } as never)
    await mount(DatabaseBackupSection)
    click('创建数据库备份'); await flush()
    expect(databaseBackupApi.create).toHaveBeenCalledOnce()
    expect(document.body.textContent).toContain('已验证')
    click('恢复到独立副本并验证'); await flush()
    expect(databaseBackupApi.restoreCopy).toHaveBeenCalledWith('backup')
    expect(document.body.textContent).toContain('恢复演练保留当前活动数据库')
    expect(document.body.textContent).toContain('project/backups/restored/copy.db')
  })
})
