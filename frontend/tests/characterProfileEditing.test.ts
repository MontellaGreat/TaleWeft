import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import CharacterProfile from '@/components/workbench/CharacterProfile.vue'
import ChapterCastManager from '@/components/workbench/ChapterCastManager.vue'

const state = vi.hoisted(() => ({
  getBible: vi.fn(), patch: vi.fn(), delete: vi.fn(), refresh: vi.fn(), analyzeOutline: vi.fn(), reload: null as null | (() => void),
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() }, dialog: { warning: vi.fn() },
}))
vi.mock('@/api/bible', () => ({ bibleApi: { getBible: state.getBible } }))
vi.mock('@/api/config', () => ({ apiClient: { patch: state.patch, delete: state.delete } }))
vi.mock('@/api/cast', () => ({ castApi: { analyzeOutline: state.analyzeOutline } }))
vi.mock('@/api/engineCore', () => ({ characterPsycheApi: { get: async () => null } }))
vi.mock('@/api/memory', () => ({ memoryApi: { getCharacterProjection: async () => null } }))
vi.mock('@/composables/useWorkbenchNarrativeSync', () => ({ useWorkbenchDeskTickReload: (reload: () => void) => { state.reload = reload } }))
vi.mock('@/stores/workbenchRefreshStore', () => ({ useWorkbenchRefreshStore: () => ({ bumpAfterChapterDeskChange: state.refresh }) }))
vi.mock('naive-ui', () => ({ useMessage: () => state.message, useDialog: () => state.dialog }))
vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn(), onBeforeRouteUpdate: vi.fn() }))

const character = () => ({ id: 'c1', name: '林舟', description: '旧角色简介', relationships: [{ target: '顾言', relation: '朋友', description: '一起旅行' }],
  gender: '男', age: '20', appearance: '黑发', personality: '谨慎', background: '来自海边', core_motivation: '找到家人', inner_lack: '信任',
  public_profile: '旅人', hidden_profile: '王子', reveal_chapter: 4, core_belief: '相信同伴', moral_taboos: ['不背叛'],
  voice_profile: { style: '简短', custom_anchor: '保留扩展字段' }, active_wounds: [{ trigger: '雨夜', effect: '沉默', description: '旧伤' }] })
const bible = (char = character()) => ({ id: 'b1', novel_id: 'book', characters: [char], world_settings: [], locations: [], timeline_notes: [], style_notes: [] })
const Box = defineComponent({ setup(_, { slots }) { return () => h('div', [slots.header?.(), slots.default?.(), slots.footer?.(), slots.action?.(), slots.trigger?.()]) } })
const Modal = defineComponent({ props: ['show'], setup(props, { slots }) { return () => props.show ? h('section', [slots.header?.(), slots.default?.(), slots.footer?.(), slots.action?.()]) : null } })
const Button = defineComponent({ props: ['disabled', 'loading'], emits: ['click'], setup(props, { slots, emit }) { return () => h('button', { disabled: props.disabled || props.loading, onClick: () => emit('click') }, slots.default?.()) } })
const Input = defineComponent({ inheritAttrs: false, props: ['value', 'disabled'], emits: ['update:value'], setup(props, { attrs, emit }) {
  return () => h('textarea', { ...attrs, value: props.value, disabled: props.disabled, onInput: (event: any) => emit('update:value', event.target.value) })
} })
const stubs: Record<string, any> = { NModal: Modal, NButton: Button, NInput: Input, NInputNumber: Input, DialogueCorpus: true }
for (const name of ['NSpin', 'NTabs', 'NTabPane', 'NIcon', 'NTooltip', 'NForm', 'NFormItem', 'NAlert', 'NSpace', 'NDivider', 'NTag', 'NSelect', 'NEmpty']) stubs[name] = Box
let wrappers: VueWrapper[] = []
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(item => item.text() === text)!
async function openEditor() {
  const wrapper = mount(CharacterProfile, { props: { slug: 'book', selectedCharacterId: 'c1' }, global: { stubs } })
  wrappers.push(wrapper)
  await flushPromises()
  expect(button(wrapper, '编辑档案'), 'the actual character profile must expose editing').toBeDefined()
  await button(wrapper, '编辑档案').trigger('click')
  await flushPromises()
  return wrapper
}
beforeEach(() => {
  vi.clearAllMocks()
  state.getBible.mockResolvedValue(bible())
  state.patch.mockResolvedValue(bible({ ...character(), name: '林川', description: '新的角色简介' }))
  state.delete.mockResolvedValue({ ...bible(), characters: [] })
})
afterEach(() => { wrappers.forEach(wrapper => wrapper.unmount()); wrappers = [] })

describe('actual character profile editing', () => {
  it('saves name and complete profile fields with the original CAS snapshot, then refreshes the desk', async () => {
    const wrapper = await openEditor()
    await wrapper.get('[data-testid="character-name"]').setValue('林川')
    await wrapper.get('[data-testid="character-description"]').setValue('新的角色简介')
    expect(wrapper.text()).toContain('未保存')
    await button(wrapper, '保存档案').trigger('click')
    await flushPromises()
    expect(state.patch).toHaveBeenCalledWith('/bible/novels/book/bible/characters/c1', {
      changes: { name: '林川', description: '新的角色简介' }, expected_character: character(),
    })
    expect(wrapper.get('.cp-id-name').text()).toBe('林川')
    expect(state.refresh).toHaveBeenCalledOnce()
  })

  it('shows saved description and identity fields in the profile itself', async () => {
    const wrapper = mount(CharacterProfile, { props: { slug: 'book', selectedCharacterId: 'c1' }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('旧角色简介')
    expect(wrapper.text()).toContain('来自海边')
    expect(wrapper.text()).toContain('找到家人')
  })

  it('edits structured relationships and voice without dropping untouched extension fields', async () => {
    const wrapper = await openEditor()
    expect(wrapper.find('[data-testid="character-role"]').exists()).toBe(false)
    await wrapper.get('[data-testid="character-description"]').setValue('')
    await wrapper.get('[aria-label="关系对象"]').setValue('顾安')
    await wrapper.get('[label="说话风格"] textarea').setValue('缓慢温和')
    await button(wrapper, '保存档案').trigger('click')
    await flushPromises()
    const payload = state.patch.mock.calls[0][1]
    expect(payload.changes).toEqual({ description: '', relationships: [{ target: '顾安', relation: '朋友', description: '一起旅行' }],
      voice_profile: { style: '缓慢温和', custom_anchor: '保留扩展字段' } })
    expect(payload.changes).not.toHaveProperty('id')
  })

  it('ignores a previous character read completing after the selection changes', async () => {
    let resolve!: (value: any) => void
    state.getBible.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const wrapper = mount(CharacterProfile, { props: { slug: 'book', selectedCharacterId: 'c1' }, global: { stubs } })
    wrappers.push(wrapper)
    await nextTick()
    state.getBible.mockResolvedValue({ ...bible(), characters: [{ ...character(), id: 'c2', name: '顾言' }] })
    await wrapper.setProps({ selectedCharacterId: 'c2' })
    await flushPromises()
    expect(wrapper.get('.cp-id-name').text()).toBe('顾言')
    resolve(bible())
    await flushPromises()
    expect(wrapper.get('.cp-id-name').text()).toBe('顾言')
  })

  it('retains local changes during background refresh and after a server conflict', async () => {
    state.patch.mockRejectedValue({ response: { status: 409, data: { detail: '角色已被其他操作修改，请刷新后重试' } } })
    const wrapper = await openEditor()
    await wrapper.get('[data-testid="character-description"]').setValue('本地草稿')
    state.getBible.mockResolvedValue(bible({ ...character(), description: '远程修改' }))
    state.reload?.()
    await flushPromises()
    expect((wrapper.get('[data-testid="character-description"]').element as HTMLTextAreaElement).value).toBe('本地草稿')
    await button(wrapper, '保存档案').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('角色已被其他操作修改')
    expect((wrapper.get('[data-testid="character-description"]').element as HTMLTextAreaElement).value).toBe('本地草稿')
    expect(state.patch).toHaveBeenCalledOnce()
    expect(state.refresh).not.toHaveBeenCalled()
  })

  it('requires confirmation before deleting and clears the selected profile after success', async () => {
    const wrapper = await openEditor()
    await button(wrapper, '删除角色').trigger('click')
    expect(state.delete).not.toHaveBeenCalled()
    const confirmation = state.dialog.warning.mock.calls[0][0]
    expect(confirmation.content).toContain('历史章节正文')
    await confirmation.onPositiveClick()
    await flushPromises()
    expect(state.delete).toHaveBeenCalledWith('/bible/novels/book/bible/characters/c1', { data: { expected_character: character() } })
    expect(wrapper.emitted('deleted')?.[0]).toEqual(['c1'])
    expect(wrapper.find('.cp-id-name').exists()).toBe(false)
  })

  it('blocks selection changes while saving and confirms discarding unsaved changes', async () => {
    let resolve!: (value: any) => void
    state.patch.mockReturnValue(new Promise(done => { resolve = done }))
    const wrapper = await openEditor()
    await wrapper.get('[data-testid="character-name"]').setValue('林川')
    const leave = (wrapper.vm as any).canLeave()
    await nextTick()
    expect(state.dialog.warning.mock.calls[0][0].negativeText).toBe('继续编辑')
    state.dialog.warning.mock.calls[0][0].onNegativeClick()
    expect(await leave).toBe(false)
    await button(wrapper, '保存档案').trigger('click')
    expect(await (wrapper.vm as any).canLeave()).toBe(false)
    expect(wrapper.get('[data-testid="character-name"]').attributes('disabled')).toBeDefined()
    resolve(bible({ ...character(), name: '林川' }))
    await flushPromises()
  })
})

describe('chapter cast reacts to durable character changes', () => {
  it('reloads current cast names on a workbench tick without applying a new cast contract', async () => {
    state.reload = null
    const result = (name: string) => ({ cast: [{ character_id: 'c1', name, importance: 'major', is_new_suggestion: false }] })
    state.analyzeOutline.mockResolvedValue(result('林舟'))
    const wrapper = mount(ChapterCastManager, { props: { slug: 'book', chapterNumber: 1 }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('林舟')
    state.analyzeOutline.mockResolvedValue(result('林川'))
    expect(state.reload, 'cast manager must listen to persisted character refreshes').toBeTypeOf('function')
    state.reload?.()
    await flushPromises()
    expect(wrapper.text()).toContain('林川')
    expect(wrapper.text()).not.toContain('林舟')
  })
})
