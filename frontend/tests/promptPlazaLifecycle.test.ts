import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import PromptDetailPanel from '@/components/workbench/promptPlaza/PromptDetailPanel.vue'
import { promptPlazaApi } from '@/api/llmControl'

vi.mock('@/api/llmControl', () => ({ promptPlazaApi: {
  getNodeDetail: vi.fn(), getNodeVersions: vi.fn(), deleteNode: vi.fn(),
} }))
vi.mock('naive-ui', () => ({
  ...Object.fromEntries(['NTag', 'NTabs', 'NTabPane', 'NInput', 'NForm', 'NFormItem', 'NButton', 'NAlert', 'NEmpty', 'NModal', 'NSpin', 'NPopconfirm'].map(name => [name, { name, template: '<div><slot /></div>' }])),
  useMessage: () => ({ success: vi.fn(), error: vi.fn() }),
}))
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { emit, slots }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Confirm = defineComponent({ emits: ['positive-click'], setup(_, { slots, emit }) {
  return () => h('div', [slots.trigger?.(), h('button', { onClick: () => emit('positive-click') }, '确认删除')])
} })
const base = { id: 'database-node-id', node_key: 'custom-test', name: 'Custom', is_builtin: false,
  system: 'system', user_template: 'user', variables: [], version_count: 1, can_delete: true,
  deletion_references: [], runtime_usage: { status: 'unbound', label: '未自动接入', description: '新建提示词不会自动用于正文生成。' },
}
async function open(overrides = {}) {
  vi.mocked(promptPlazaApi.getNodeDetail).mockResolvedValue({ ...base, ...overrides } as any)
  vi.mocked(promptPlazaApi.getNodeVersions).mockResolvedValue([] as any)
  const wrapper = mount(PromptDetailPanel, { props: { nodeKey: base.node_key }, global: { stubs: {
    NTag: Box, NTabs: Box, NTabPane: Box, NInput: Box, NForm: Box, NFormItem: Box,
    NButton: Button, NAlert: Box, NEmpty: Box, NModal: Box, NSpin: Box, NPopconfirm: Confirm,
  } } })
  await flushPromises()
  return wrapper
}
beforeEach(() => vi.resetAllMocks())

describe('prompt plaza lifecycle', () => {
  it('offers confirmed deletion for a custom prompt using its database id and emits deleted only after success', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('删除提示词')
    expect(wrapper.text()).toContain('不会自动')
    await wrapper.findAll('button').find(button => button.text() === '确认删除')!.trigger('click')
    await flushPromises()
    expect(promptPlazaApi.deleteNode).toHaveBeenCalledWith(base.id)
    expect(wrapper.emitted('deleted')).toEqual([[]])
    wrapper.unmount()
  })
  it('protects built-in prompts and displays dependency reasons for a referenced custom prompt', async () => {
    const builtin = await open({ is_builtin: true, can_delete: false })
    expect(builtin.text()).not.toContain('删除提示词')
    builtin.unmount()
    const referenced = await open({ can_delete: false, deletion_references: [{ kind: 'prompt_bindings', label: '工作流绑定', count: 1 }] })
    expect(referenced.text()).toContain('工作流绑定')
    expect(referenced.findAll('button').find(button => button.text() === '删除提示词')!.attributes('disabled')).toBeDefined()
    referenced.unmount()
  })
  it('retains the panel when deletion conflicts with a new reference', async () => {
    vi.mocked(promptPlazaApi.deleteNode).mockRejectedValue(new Error('引用已变化'))
    const wrapper = await open()
    await wrapper.findAll('button').find(button => button.text() === '确认删除')!.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('deleted')).toBeUndefined()
    wrapper.unmount()
  })
})
