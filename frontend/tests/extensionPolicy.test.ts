import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App, type Component } from 'vue'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import SignedPluginsSection from '../src/components/settings/sections/SignedPluginsSection.vue'
import QualityRulesSection from '../src/components/settings/sections/QualityRulesSection.vue'
import { extensionPolicyApi, type QualityRulesOverview, type PluginPackage, type PluginPreview } from '../src/api/extensionPolicy'

vi.mock('../src/api/extensionPolicy', async importOriginal => ({ ...await importOriginal<typeof import('../src/api/extensionPolicy')>(), extensionPolicyApi: {
  plugins: vi.fn(), trust: vi.fn(), revoke: vi.fn(), verify: vi.fn(), install: vi.fn(), uninstall: vi.fn(),
  rules: vi.fn(), publish: vi.fn(), rollback: vi.fn(), scan: vi.fn(),
} }))

const pkg = { manifest: { schema_version: 1, id: 'style', name: '文风包', version: '1.0.0', signer_id: 'publisher', permissions: ['quality_rules'] }, payload: { prompts: [], quality_rules: [] }, signature: 'signature' } satisfies PluginPackage
const preview = { id: 'style', name: '文风包', version: '1.0.0', signer_id: 'publisher', permissions: ['quality_rules'], fingerprint: 'fingerprint', digest: 'checked-digest', prompt_count: 0, rule_count: 1, verified: true } satisfies PluginPreview
const version = { id: 'current-policy', config: { disabled_builtin: [], rules: [] }, note: '作者规则', source: 'author', created_at: 'today' }
const rulesState = { current: version, versions: [version], effective: { version: 'digest', policy_version: version.id, patterns: [], plugins: [] }, builtin: [] } satisfies QualityRulesOverview
let app: App | null = null, host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
function button(label: string) { const node = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label); if (!node) throw new Error(`Missing ${label}`); return node }
function input(label: string, value: string) { const labelled = document.querySelector<HTMLElement>(`[aria-label="${label}"]`); const field = labelled?.matches('input,textarea') ? labelled as HTMLInputElement : labelled?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input,textarea'); if (!field) throw new Error(`Missing input ${label}`); field.value = value; field.dispatchEvent(new Event('input', { bubbles: true })) }
async function mount(component: Component) { host = document.createElement('div'); document.body.append(host); app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(NDialogProvider, {}, { default: () => h(component) }) }) }); app.mount(host); await flush() }
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })

describe('signed plugin and quality policy author actions', () => {
  it('only installs verified content and invalidates verification after edits', async () => {
    vi.mocked(extensionPolicyApi.plugins).mockResolvedValue({ keys: [], packages: [] })
    vi.mocked(extensionPolicyApi.verify).mockResolvedValue(preview)
    vi.mocked(extensionPolicyApi.install).mockResolvedValue(preview)
    await mount(SignedPluginsSection)
    expect(button('安装已验证插件').disabled).toBe(true)
    input('签名插件包', JSON.stringify(pkg)); await flush()
    button('验证签名与权限').click(); await flush()
    expect(document.body.textContent).toContain('文风规则')
    expect(button('安装已验证插件').disabled).toBe(false)
    input('签名插件包', JSON.stringify({ ...pkg, signature: 'changed' })); await flush()
    expect(button('安装已验证插件').disabled).toBe(true)
    expect(extensionPolicyApi.install).not.toHaveBeenCalled()
    input('签名插件包', JSON.stringify(pkg)); await flush(); button('验证签名与权限').click(); await flush()
    button('安装已验证插件').click(); await flush()
    expect(extensionPolicyApi.install).toHaveBeenCalledWith(pkg, preview.digest)
  })

  it('shows signature rejection and never enables installation', async () => {
    vi.mocked(extensionPolicyApi.plugins).mockResolvedValue({ keys: [], packages: [] })
    vi.mocked(extensionPolicyApi.verify).mockRejectedValue(new Error('插件签名验证失败'))
    await mount(SignedPluginsSection); input('签名插件包', JSON.stringify(pkg)); await flush(); button('验证签名与权限').click(); await flush()
    expect(document.body.textContent).toContain('插件签名验证失败')
    expect(button('安装已验证插件').disabled).toBe(true)
    expect(extensionPolicyApi.install).not.toHaveBeenCalled()
  })

  it('asks before revocation that removes publisher packages', async () => {
    vi.mocked(extensionPolicyApi.plugins).mockResolvedValue({ keys: [{ id: 'publisher', name: '发布者', public_key: 'key', fingerprint: 'fingerprint', permissions: ['quality_rules'] }], packages: [] })
    await mount(SignedPluginsSection); button('移除信任').click(); await flush()
    expect(extensionPolicyApi.revoke).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('同时卸载其插件')
    button('确认移除信任').click(); await flush()
    expect(extensionPolicyApi.revoke).toHaveBeenCalledWith('publisher')
  })

  it('publishes with the displayed version and exposes conflicts', async () => {
    vi.mocked(extensionPolicyApi.rules).mockResolvedValue(rulesState)
    vi.mocked(extensionPolicyApi.publish).mockRejectedValue(new Error('规则版本已变化，请刷新后保存'))
    await mount(QualityRulesSection); input('版本说明', '新规则'); await flush(); button('保存新规则版本').click(); await flush()
    expect(extensionPolicyApi.publish).toHaveBeenCalledWith({ disabled_builtin: [], rules: [] }, version.id, '新规则')
    expect(document.body.textContent).toContain('规则版本已变化')
  })

  it('uses the saved policy for a scan and shows its rule digest', async () => {
    vi.mocked(extensionPolicyApi.rules).mockResolvedValue(rulesState)
    vi.mocked(extensionPolicyApi.scan).mockResolvedValue({ rule_version: 'real-rule-digest', content_hash: 'body', metrics: { total_hits: 1, critical_hits: 1, severity_score: 30, overall_assessment: '严重' }, hits: [{ pattern: '自定义规则', text: '正文词语', replacement_hint: '修改动作' }] })
    await mount(QualityRulesSection); input('试扫正文', '正文词语'); await flush(); button('试扫当前规则').click(); await flush()
    expect(extensionPolicyApi.scan).toHaveBeenCalledWith('正文词语')
    expect(document.body.textContent).toContain('real-rule-digest')
    expect(document.body.textContent).toContain('正文词语 修改动作')
  })

  it('confirms a rule rollback and sends the displayed current policy', async () => {
    vi.mocked(extensionPolicyApi.rules).mockResolvedValue({ ...rulesState, versions: [version, { ...version, id: 'old-policy', note: '旧规则', created_at: 'yesterday' }] })
    vi.mocked(extensionPolicyApi.rollback).mockResolvedValue({ ...version, id: 'restored-policy' })
    await mount(QualityRulesSection)
    const selection = document.querySelector('[aria-label="要恢复的规则版本"]')
    const trigger = selection?.querySelector<HTMLElement>('.n-base-selection') ?? selection as HTMLElement | null
    if (!trigger) throw new Error('Missing restore selection')
    trigger.click(); await flush()
    const option = [...document.querySelectorAll<HTMLElement>('.n-base-select-option')].find(item => item.textContent?.includes('旧规则'))
    if (!option) throw new Error('Missing old rule version')
    option.click(); await flush()
    button('恢复此版本').click(); await flush()
    expect(extensionPolicyApi.rollback).not.toHaveBeenCalled()
    button('确认恢复规则').click(); await flush()
    expect(extensionPolicyApi.rollback).toHaveBeenCalledWith('old-policy', version.id)
  })
})
