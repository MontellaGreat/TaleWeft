import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import ManuscriptTransferSection from '../src/components/settings/sections/ManuscriptTransferSection.vue'
import { manuscriptApi, type ManuscriptPreview } from '../src/api/manuscript'
import { novelApi } from '../src/api/novel'

vi.mock('../src/api/manuscript', () => ({ manuscriptApi: { preview: vi.fn(), get: vi.fn(), confirm: vi.fn() } }))
vi.mock('../src/api/novel', () => ({ novelApi: { listNovels: vi.fn(), exportNovel: vi.fn() } }))
const preview = { preview_id: 'preview', new_novel_id: 'new', status: 'pending', expired: false, payload_sha256: 'hash',
  novel: { title: '导入的新作品', author: '作者', premise: '前提' }, validation: { status: 'valid', chapter_count: 1,
    content_character_count: 12, empty_chapter_count: 0, input_sha256: 'input', warnings: [], errors: [], chapters: [{ number: 1, title: '首章', characters: 12, content_sha256: 'body' }] }, receipt: null,
} satisfies ManuscriptPreview
let app: App | null = null, host: HTMLDivElement | null = null
async function flush() { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
function click(label: string) { const node = [...document.querySelectorAll('button')].find(item => item.textContent?.trim() === label); if (!node) throw new Error(`Missing ${label}`); node.click() }
async function mount() {
  vi.mocked(novelApi.listNovels).mockResolvedValue([])
  host = document.createElement('div'); document.body.append(host)
  app = createApp({ render: () => h(NMessageProvider, {}, { default: () => h(NDialogProvider, {}, { default: () => h(ManuscriptTransferSection) }) }) })
  app.mount(host); await flush()
}
async function chooseFile(content = '{"manuscript":"test"}') {
  const input = document.querySelector('input[type=file]') as HTMLInputElement
  Object.defineProperty(input, 'files', { configurable: true, value: [{ name: 'story.plotpilot.json', size: content.length, text: async () => content }] })
  input.dispatchEvent(new Event('change')); await flush()
}
afterEach(() => { app?.unmount(); host?.remove(); app = null; host = null; document.body.innerHTML = ''; localStorage.clear(); vi.clearAllMocks() })
describe('manuscript import author workflow', () => {
  it('validates first, requires confirmation, and submits the displayed hash', async () => {
    vi.mocked(manuscriptApi.preview).mockResolvedValue(preview)
    vi.mocked(manuscriptApi.confirm).mockResolvedValue({ status: 'imported', preview_id: 'preview', novel_id: 'new', title: '导入的新作品', chapter_count: 1, content_character_count: 12, payload_sha256: 'hash', original_novels_changed: false })
    await mount(); await chooseFile()
    click('校验并预览导入'); await flush()
    expect(document.body.textContent).toContain('已通过稿件校验')
    expect(manuscriptApi.confirm).not.toHaveBeenCalled()
    click('确认导入为新作品'); await flush()
    expect(manuscriptApi.confirm).not.toHaveBeenCalled()
    click('创建新作品并导入'); await flush()
    expect(manuscriptApi.confirm).toHaveBeenCalledWith(preview)
    expect(document.body.textContent).toContain('原作品保留')
  })
  it('shows schema errors without offering confirmation', async () => {
    const failure = Object.assign(new Error('校验失败'), { response: { data: { detail: { errors: ['章节[1]正文SHA-256校验不匹配'] } } } })
    vi.mocked(manuscriptApi.preview).mockRejectedValue(failure)
    await mount(); await chooseFile(); click('校验并预览导入'); await flush()
    expect(document.body.textContent).toContain('正文SHA-256校验不匹配')
    expect(document.body.textContent).not.toContain('确认导入为新作品')
  })
  it('reloads the saved preview receipt after reconnecting', async () => {
    localStorage.setItem('plotpilot:manuscript-import-preview', 'preview')
    vi.mocked(manuscriptApi.get).mockResolvedValue(preview)
    await mount()
    expect(manuscriptApi.get).toHaveBeenCalledWith('preview')
    expect(document.body.textContent).toContain('导入的新作品')
  })
})
