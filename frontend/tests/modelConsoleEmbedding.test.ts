import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import Console from '@/components/global/GlobalLLMEntryButton.vue'
import { settingsApi, type InstallEvent } from '@/api/settings'

vi.mock('@/components/workbench/LLMControlPanel.vue', () => ({ default: { template: '<div>LLM settings</div>' } }))
vi.mock('@/api/settings', () => ({ settingsApi: {
  getEmbeddingConfig: vi.fn(), getExtensionsStatus: vi.fn(), updateEmbeddingConfig: vi.fn(),
  fetchEmbeddingModels: vi.fn(), installExtensions: vi.fn(),
} }))
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', [slots.header?.(), slots.default?.(), slots.footer?.()]) } })
const Modal = defineComponent({ props: ['show'], setup(props, { slots }) { return () => props.show ? h('section', [slots.header?.(), slots.default?.(), slots.footer?.()]) : null } })
const Button = defineComponent({ props: ['disabled', 'loading'], emits: ['click'], setup(props, { slots, emit }) { return () => h('button', { disabled: props.disabled || props.loading, onClick: () => emit('click') }, slots.default?.()) } })
const stubs = Object.fromEntries(['NTag', 'NSwitch', 'NForm', 'NFormItem', 'NInput', 'NSelect', 'NSpin', 'NAlert', 'NProgress'].map(name => [name, Box]))
let wrapper: VueWrapper
const localConfig = { mode: 'local' as const, api_key: '', base_url: '', model: '', model_path: 'models/bge-small-zh-v1.5', use_gpu: true }
const status = { faiss: true, numpy: true, sentence_transformers: true, all_installed: true, install_running: false, install_progress: '', model_ready: false, local_ready: false, model_path: 'models/bge-small-zh-v1.5', download_source: '' }
let handlers: { onEvent?: (e: InstallEvent) => void; onDone?: (success: boolean) => void; onError?: (error: Error) => void }
const abort = vi.fn()
async function open() {
  wrapper = mount(Console, { global: { stubs: { ...stubs, NModal: Modal, NButton: Button, Teleport: true } } })
  ;(wrapper.vm as any).openPanel('embedding')
  await flushPromises()
}
function button(text: string) { return wrapper.findAll('button').find(item => item.text().includes(text))! }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(settingsApi.getEmbeddingConfig).mockResolvedValue({ ...localConfig } as any)
  vi.mocked(settingsApi.getExtensionsStatus).mockResolvedValue({ ...status } as any)
  vi.mocked(settingsApi.installExtensions).mockImplementation(value => { handlers = value; return { abort } as any })
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = ''; vi.useRealTimers() })

describe('model console local embedding preparation', () => {
  it('opens the requested embedding tab and offers model download even when dependencies are installed', async () => {
    await open()
    expect(wrapper.text()).toContain('模型控制台')
    expect(wrapper.text()).toContain('本地模型文件尚未就绪')
    expect(button('下载并准备本地模型')).toBeDefined()
    expect(settingsApi.getEmbeddingConfig).toHaveBeenCalledTimes(1)
  })
  it('shows selected source and the real terminal error without calling it a disconnected stream', async () => {
    await open(); await button('下载并准备本地模型').trigger('click')
    handlers.onEvent?.({ type: 'info', message: '已选择国内下载源：ModelScope' })
    handlers.onEvent?.({ type: 'error', message: '镜像均不可达，请检查网络' })
    handlers.onDone?.(false)
    await flushPromises()
    expect(wrapper.text()).toContain('ModelScope')
    expect(wrapper.text()).toContain('镜像均不可达')
    expect(wrapper.text()).not.toContain('连接中断')
    expect(button('下载并准备本地模型').attributes('disabled')).toBeUndefined()
  })
  it('refreshes completion status without overwriting configuration', async () => {
    await open(); await button('下载并准备本地模型').trigger('click')
    vi.mocked(settingsApi.getExtensionsStatus).mockResolvedValue({ ...status, model_ready: true, local_ready: true, download_source: 'ModelScope' } as any)
    handlers.onDone?.(true); await flushPromises()
    expect(wrapper.text()).toContain('本地嵌入模型已就绪')
    expect(settingsApi.updateEmbeddingConfig).not.toHaveBeenCalled()
  })
  it('ignores late completion after explicit cancellation', async () => {
    await open(); await button('下载并准备本地模型').trigger('click')
    await button('取消').trigger('click')
    const statusChecks = vi.mocked(settingsApi.getExtensionsStatus).mock.calls.length
    handlers.onDone?.(true); await flushPromises()
    expect(abort).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).not.toContain('本地模型准备完成')
    expect(settingsApi.getExtensionsStatus).toHaveBeenCalledTimes(statusChecks)
  })
  it('shows a status failure and keeps a retry action', async () => {
    vi.mocked(settingsApi.getExtensionsStatus).mockRejectedValue(new Error('status offline'))
    await open()
    expect(wrapper.text()).toContain('status offline')
    expect(button('重新检测')).toBeDefined()
    vi.mocked(settingsApi.getExtensionsStatus).mockResolvedValue({ ...status, local_ready: true, model_ready: true } as any)
    await button('重新检测').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('status offline')
    expect(wrapper.text()).toContain('本地嵌入模型已就绪')
  })
  it('ignores an old config response after the author changes to a cloud draft', async () => {
    let resolve!: (value: any) => void
    vi.mocked(settingsApi.getEmbeddingConfig).mockReturnValue(new Promise(done => { resolve = done }))
    await open()
    const setup = (wrapper.vm as any).$.setupState
    setup.embeddingForm = { ...localConfig, mode: 'openai', model: 'my-embedding', base_url: 'https://my.example/v1' }
    resolve({ ...localConfig }); await flushPromises()
    expect(setup.embeddingForm.mode).toBe('openai')
    expect(setup.embeddingForm.model).toBe('my-embedding')
  })
  it('keeps edits made while a save request is pending', async () => {
    await open()
    let resolve!: (value: any) => void
    vi.mocked(settingsApi.updateEmbeddingConfig).mockReturnValue(new Promise(done => { resolve = done }))
    await button('保存嵌入配置').trigger('click')
    const setup = (wrapper.vm as any).$.setupState
    setup.embeddingForm.model_path = 'D:/custom-model'
    resolve({ ...localConfig }); await flushPromises()
    expect(setup.embeddingForm.model_path).toBe('D:/custom-model')
  })
  it('does not show ready for a newly entered different model path', async () => {
    vi.mocked(settingsApi.getExtensionsStatus).mockResolvedValue({ ...status, model_ready: true, local_ready: true } as any)
    await open()
    ;(wrapper.vm as any).$.setupState.embeddingForm.model_path = 'D:/missing-model'
    await flushPromises()
    expect(wrapper.text()).not.toContain('本地嵌入模型已就绪')
    expect(wrapper.text()).toContain('本地模型文件尚未就绪')
  })
  it('shows cloud save errors in the active panel', async () => {
    await open()
    ;(wrapper.vm as any).$.setupState.embeddingForm.mode = 'openai'
    vi.mocked(settingsApi.updateEmbeddingConfig).mockRejectedValue(new Error('cloud save failed'))
    await button('保存嵌入配置').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('cloud save failed')
  })
  it('automatically refreshes a background dependency check', async () => {
    vi.useFakeTimers()
    vi.mocked(settingsApi.getExtensionsStatus).mockResolvedValueOnce({ ...status, checking: true } as any)
      .mockResolvedValueOnce({ ...status, checking: false, local_ready: true, model_ready: true } as any)
    await open()
    expect(wrapper.text()).toContain('正在验证本地依赖')
    await vi.advanceTimersByTimeAsync(1200); await flushPromises()
    expect(wrapper.text()).toContain('本地嵌入模型已就绪')
    expect(settingsApi.getExtensionsStatus).toHaveBeenCalledTimes(2)
  })
})
