import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { NMessageProvider } from 'naive-ui'
import CredentialManagementSection from '../src/components/settings/sections/CredentialManagementSection.vue'
import { settingsApi, type CredentialReference } from '../src/api/settings'

vi.mock('../src/api/settings', async () => {
  const actual = await vi.importActual<typeof import('../src/api/settings')>('../src/api/settings')
  return { ...actual, settingsApi: { listCredentials: vi.fn(), exportCredentials: vi.fn(), deleteCredential: vi.fn() } }
})

const active: CredentialReference = { credential_ref: 'ppcred:v1:' + 'a'.repeat(32), ciphertext_sha256: 'hash-a', size_bytes: 10, created_at: 'today', updated_at: 'today', status: 'active', owners: [{ table: 'llm_profiles', record_id_sha256: 'owner', field: 'api_key' }] }
const orphan: CredentialReference = { credential_ref: 'ppcred:v1:' + 'b'.repeat(32), ciphertext_sha256: 'hash-b', size_bytes: 10, created_at: 'today', updated_at: 'today', status: 'orphaned', owners: [] }
let app: App | null = null
let host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; vi.clearAllMocks() })

describe('credential management', () => {
  it('shows lifecycle state and only enables deletion for orphaned refs', async () => {
    vi.mocked(settingsApi.listCredentials).mockResolvedValue({ format: 'plotpilot-credential-refs-v1', credentials: [active, orphan] })
    host = document.createElement('div'); document.body.append(host)
    app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(CredentialManagementSection) }) })
    app.mount(host); await flush()
    expect(document.body.textContent).toContain('当前引用中')
    expect(document.body.textContent).toContain('未被引用，可清理')
    const buttons = [...document.querySelectorAll('button')].filter(button => button.textContent?.includes('安全删除')) as HTMLButtonElement[]
    expect(buttons).toHaveLength(2)
    expect(buttons[0].disabled).toBe(true)
    expect(buttons[1].disabled).toBe(false)
  })
})
