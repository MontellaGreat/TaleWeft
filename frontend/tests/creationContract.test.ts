import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import BiblePanel from '../src/components/panels/BiblePanel.vue'

const fixture = vi.hoisted(() => ({
  get: vi.fn(), save: vi.fn(), bibleWrite: vi.fn(), message: { success: vi.fn(), error: vi.fn() },
}))
vi.mock('../src/api/creationContract', () => ({ creationContractApi: { get: fixture.get, save: fixture.save } }))
vi.mock('../src/api/bible', () => ({ bibleApi: { getBible: vi.fn().mockResolvedValue({}), updateBible: fixture.bibleWrite } }))
vi.mock('../src/api/novel', () => ({ novelApi: { getNovel: vi.fn().mockResolvedValue({}) } }))
vi.mock('../src/api/knowledge', () => ({ knowledgeApi: { getKnowledge: vi.fn().mockResolvedValue({}) } }))
vi.mock('naive-ui', () => ({ useMessage: () => fixture.message }))

const contract = () => ({ locked_genre: '玄幻 / 作者主题', locked_world_preset: '完整世界基调'.repeat(80),
  locked_story_structure: '三幕结构', locked_pacing_control: '循序渐进', locked_writing_style: '完整写作规则',
  locked_special_requirements: '结局要有回声', target_chapters: 100, target_words_per_chapter: 3000,
  style_notes: '作者自定义的完整文风公约'.repeat(30), premise_lock: '完整梗概' })
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const Input = defineComponent({ inheritAttrs: false, props: ['value', 'disabled', 'inputProps'], emits: ['update:value'],
  setup(props, { emit, attrs }) { return () => h('textarea', { ...attrs, ...props.inputProps, disabled: props.disabled, value: props.value,
    onInput: (event: any) => emit('update:value', event.target.value) }) } })
const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { emit, slots }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Checkbox = defineComponent({ props: ['checked', 'disabled'], emits: ['update:checked'], setup(props, { emit }) {
  return () => h('input', { type: 'checkbox', checked: props.checked, disabled: props.disabled,
    onChange: (event: any) => emit('update:checked', event.target.checked) })
} })
const stubs: Record<string, any> = { NInput: Input, NInputNumber: Input, NButton: Button, NModal: true,
  StylePresetSelector: true, NCheckbox: Checkbox }
for (const name of ['NIcon', 'NTag', 'NAlert', 'NSpace']) stubs[name] = Box
let wrapper: VueWrapper | undefined
beforeEach(() => {
  vi.clearAllMocks()
  fixture.get.mockResolvedValue({ contract: contract(), version: 'v1', structured_market: false })
  fixture.save.mockImplementation(async (_slug: string, request: any) => ({ contract: request.contract, version: 'v2', structured_market: false }))
})
afterEach(() => wrapper?.unmount())

it('shows all contract text and saves editable values through its versioned endpoint', async () => {
  wrapper = mount(BiblePanel, { props: { slug: 'book' }, global: { stubs } })
  await flushPromises()
  const world = wrapper.find('textarea[aria-label="世界基调"]')
  expect(world.exists()).toBe(true)
  expect((world.element as HTMLTextAreaElement).value).toBe(contract().locked_world_preset)
  expect(wrapper.find('textarea[aria-label="文风公约"]').exists()).toBe(true)
  await world.setValue('修改后的完整世界基调')
  const saved = { ...contract(), locked_world_preset: '修改后的完整世界基调' }
  fixture.get.mockResolvedValue({ contract: saved, version: 'v2', structured_market: false })
  await wrapper.findAll('button').find(button => button.text() === '保存')!.trigger('click')
  await flushPromises()
  expect(fixture.save).toHaveBeenCalledWith('book', { version: 'v1', contract: saved, confirm_market_profile: false })
  expect(fixture.get).toHaveBeenCalledTimes(2)
  expect(fixture.bibleWrite).not.toHaveBeenCalled()
  expect(fixture.message.success).toHaveBeenCalled()
})

it('keeps edits and reports a stale-version rejection without claiming success', async () => {
  wrapper = mount(BiblePanel, { props: { slug: 'book' }, global: { stubs } })
  await flushPromises()
  await wrapper.find('textarea[aria-label="硬性要求"]').setValue('保留尚未保存的要求')
  fixture.save.mockRejectedValue({ response: { status: 409, data: { detail: { reason: '创作契约已更新' } } } })
  await wrapper.findAll('button').find(button => button.text() === '保存')!.trigger('click')
  await flushPromises()
  expect(fixture.message.success).not.toHaveBeenCalled()
  expect(fixture.message.error).toHaveBeenCalled()
  expect((wrapper.find('textarea[aria-label="硬性要求"]').element as HTMLTextAreaElement).value).toBe('保留尚未保存的要求')
})

it('requires confirmation of the current five dimensions and resets it after another edit', async () => {
  fixture.get.mockResolvedValue({ contract: contract(), version: 'v1', structured_market: true })
  wrapper = mount(BiblePanel, { props: { slug: 'book' }, global: { stubs } })
  await flushPromises()
  await wrapper.find('textarea[aria-label="世界基调"]').setValue('新的完整世界基调')
  const button = () => wrapper!.findAll('button').find(item => item.text() === '保存')!
  expect(button().attributes('disabled')).toBeDefined()
  await wrapper.find('input[type="checkbox"]').setValue(true)
  expect(button().attributes('disabled')).toBeUndefined()
  await wrapper.find('textarea[aria-label="节奏控制"]').setValue('新的节奏控制')
  expect(button().attributes('disabled')).toBeDefined()
  expect(fixture.save).not.toHaveBeenCalled()
})
