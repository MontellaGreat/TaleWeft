import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import SoftwareUpdateSection from '../src/components/settings/sections/SoftwareUpdateSection.vue'
import { softwareUpdateApi } from '../src/api/softwareUpdate'
import { prepareAndInstallUpdate, registerUpdateSaveBarrier, updatePreparing } from '../src/lifecycle/updatePreparation'

vi.mock('../src/api/softwareUpdate', () => ({ softwareUpdateApi: { status: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn() } }))
const verified = { available: true, stage: 'verified' as const, version: '4.7.0', notes: 'Release notes', message: '已验证' }
let app: App | undefined
let host: HTMLElement | undefined
async function flush() { for (let i = 0; i < 4; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
async function mount() {
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(SoftwareUpdateSection) }); app.mount(host); await flush()
}
function click(label: string) {
  const button = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!button) throw new Error(`Missing ${label}`)
  button.click()
}
afterEach(() => { app?.unmount(); host?.remove(); app = undefined; host = undefined; vi.clearAllMocks() })

describe('explicit signed updater author flow', () => {
  it('opening settings only reads local status and shows unavailable builds', async () => {
    vi.mocked(softwareUpdateApi.status).mockResolvedValue({ ...verified, available: false, stage: 'unavailable', message: '未启用签名更新' })
    await mount()
    expect(document.body.textContent).toContain('未启用签名更新')
    expect(softwareUpdateApi.check).not.toHaveBeenCalled()
    expect(softwareUpdateApi.download).not.toHaveBeenCalled()
    expect(softwareUpdateApi.install).not.toHaveBeenCalled()
  })
  it('does not enable install before explicit author confirmation', async () => {
    vi.mocked(softwareUpdateApi.status).mockResolvedValue(verified)
    await mount(); click('保存并安装更新'); await flush()
    expect(softwareUpdateApi.install).not.toHaveBeenCalled()
  })
  it('keeps failed signature packages out of the install state', async () => {
    vi.mocked(softwareUpdateApi.status).mockResolvedValue({ ...verified, stage: 'available' })
    vi.mocked(softwareUpdateApi.download).mockRejectedValue(new Error('invalid signature'))
    await mount(); click('下载并验证签名'); await flush()
    expect(document.body.textContent).toContain('invalid signature')
    expect(document.body.textContent).not.toContain('保存并安装更新')
    expect(softwareUpdateApi.install).not.toHaveBeenCalled()
  })
  it('failed save barrier blocks backend shutdown and install', async () => {
    const remove = registerUpdateSaveBarrier(async () => false)
    const install = vi.fn()
    try { await expect(prepareAndInstallUpdate(install)).rejects.toThrow('未成功保存') }
    finally { remove() }
    expect(install).not.toHaveBeenCalled()
    expect(updatePreparing.value).toBe(false)
  })
  it('awaits every save before invoking install and prevents concurrent installation', async () => {
    const calls: string[] = []
    let release!: () => void
    const remove = registerUpdateSaveBarrier(async () => { calls.push('save'); await new Promise<void>(resolve => { release = resolve }); return true })
    const task = prepareAndInstallUpdate(async () => { calls.push('install') })
    expect(updatePreparing.value).toBe(true)
    await expect(prepareAndInstallUpdate(async () => {})).rejects.toThrow('正在进行')
    expect(calls).toEqual(['save']); release(); await task; remove()
    expect(calls).toEqual(['save', 'install'])
    expect(updatePreparing.value).toBe(false)
  })
  it('propagates refused backend preparation and releases the editing guard', async () => {
    await expect(prepareAndInstallUpdate(async () => { throw new Error('writers not drained') })).rejects.toThrow('writers not drained')
    expect(updatePreparing.value).toBe(false)
  })
})
