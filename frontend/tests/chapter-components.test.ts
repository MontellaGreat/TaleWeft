import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, reactive } from 'vue'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ChapterView from '../src/views/Chapter.vue'
import HistoryModal from '../src/components/workbench/ChapterHistoryModal.vue'

const state = vi.hoisted(() => ({
  api: { listChapters: vi.fn(), getChapter: vi.fn(), getChapterReview: vi.fn(), getChapterStructure: vi.fn(), updateChapter: vi.fn(),
    listVersions: vi.fn(), listDerivationJobs: vi.fn(), diffVersion: vi.fn(), restoreVersion: vi.fn(), requestDerivation: vi.fn(),
    listDrafts: vi.fn(), diffDraft: vi.fn(), restoreDraft: vi.fn() },
  route: null as any, leave: null as null | (() => Promise<boolean>), update: null as any,
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }, dialog: { warning: vi.fn() }, stats: vi.fn(),
}))
vi.mock('../src/api/chapter', () => ({ chapterApi: state.api, getChapterRevisionConflict: (error: any) => error?.conflict || null }))
vi.mock('../src/api/knowledgeGraph', () => ({ knowledgeGraphApi: { getChapterInferenceEvidence: async () => ({ data: { facts: [], story_node_id: null } }) } }))
vi.mock('../src/stores/statsStore', () => ({ useStatsStore: () => ({ onChapterSaved: state.stats }) }))
vi.mock('naive-ui', () => ({ useMessage: () => state.message, useDialog: () => state.dialog }))
vi.mock('vue-router', () => ({ useRoute: () => state.route, useRouter: () => ({ push: vi.fn() }),
  onBeforeRouteLeave: (guard: any) => { state.leave = guard }, onBeforeRouteUpdate: (guard: any) => { state.update = guard } }))

const version = (revision: number) => ({ revision, content_hash: `h${revision}`, etag: `etag${revision}` })
const chapter = (content = 'baseline', revision = 1, number = 1) => ({ id: 'c', novel_id: 'book', number,
  title: 'Chapter', content, ...version(revision), status: 'draft', created_at: '2026-10-12', updated_at: '2026-10-12' })
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: any) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail }); return { promise, resolve, reject } }
const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', Object.keys(slots).map(name => slots[name]?.())) } })
const Button = defineComponent({ props: ['disabled', 'loading'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value', 'disabled'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('textarea', { value: props.value, disabled: props.disabled, onInput: (e: any) => emit('update:value', e.target.value) })
} })
const Select = defineComponent({ props: ['value', 'options'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('select', { value: props.value, onChange: (e: any) => {
    const raw = e.target.value
    emit('update:value', raw !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw)
  } },
    (props.options || []).map((option: any) => h('option', { value: option.value }, option.label)))
} })
const stubs: Record<string, any> = { NInput: Input, NButton: Button, NSelect: Select, ChapterHistoryModal: true }
for (const name of ['NSpin', 'NSpace', 'NTag', 'NDivider', 'NButtonGroup', 'NDropdown', 'NSplit', 'NCard', 'NText', 'NAlert', 'NRadioGroup',
  'NRadio', 'NModal', 'NTabs', 'NTabPane', 'NCollapse', 'NCollapseItem', 'NEmpty', 'NTooltip']) stubs[name] = Box
let wrappers: VueWrapper[] = []

beforeEach(() => {
  vi.clearAllMocks()
  state.route = reactive({ name: 'Chapter', params: { slug: 'book', id: '1' } })
  state.api.getChapter.mockResolvedValue(chapter())
  state.api.listChapters.mockResolvedValue([chapter(), chapter('second', 4, 2)])
  state.api.getChapterReview.mockResolvedValue({ status: 'draft', memo: '' })
  state.api.getChapterStructure.mockResolvedValue({ word_count: 8, paragraph_count: 1, dialogue_ratio: 0, scene_count: 1, pacing: 'normal' })
  state.api.listVersions.mockResolvedValue([{ ...chapter('old', 0), title: '旧章节名', version_id: 'old', source: 'initial' }, { ...chapter(), version_id: 'new', source: 'manual' }])
  state.api.listDerivationJobs.mockResolvedValue([])
  state.api.diffVersion.mockResolvedValue({ diff: 'version diff' })
  state.api.listDrafts.mockResolvedValue([])
  state.api.diffDraft.mockResolvedValue({ diff: 'draft diff' })
  sessionStorage.clear()
})
afterEach(() => { wrappers.forEach(wrapper => wrapper.unmount()); wrappers = []; sessionStorage.clear() })
const wrap = (component: any, props: any = {}) => { const wrapper = mount(component, { props, global: { stubs } }); wrappers.push(wrapper); return wrapper }
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(candidate => candidate.text() === text)!

describe('actual chapter editor component', () => {
  it('waits for in-flight save and later keystrokes before allowing route leave', async () => {
    const first = deferred<any>(), second = deferred<any>()
    state.api.updateChapter.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const wrapper = wrap(ChapterView)
    await flushPromises()
    const editor = wrapper.find('textarea.content-editor')
    await editor.setValue('first edit')
    await button(wrapper, '保存').trigger('click')
    await editor.setValue('typed during save')
    let allowed = false
    const leaving = state.leave!().then(result => { allowed = result; return result })
    expect(state.api.updateChapter).toHaveBeenCalledTimes(1)
    first.resolve(chapter('first edit', 2))
    await flushPromises()
    expect(allowed).toBe(false)
    expect(state.api.updateChapter).toHaveBeenCalledTimes(2)
    expect(state.api.updateChapter.mock.calls[1][2].content).toBe('typed during save')
    expect(state.api.updateChapter.mock.calls[1][3].revision).toBe(2)
    second.resolve(chapter('typed during save', 3))
    expect(await leaving).toBe(true)
    expect((editor.element as HTMLTextAreaElement).value).toBe('typed during save')
  })

  it('409 blocks navigation and keeps the local editor until an explicit server reload', async () => {
    const conflict = { current: chapter('server text', 7), diff: 'server/local diff' }
    state.api.updateChapter.mockRejectedValue({ conflict })
    const wrapper = wrap(ChapterView)
    await flushPromises()
    const editor = wrapper.find('textarea.content-editor')
    await editor.setValue('local text')
    expect(await state.leave!()).toBe(false)
    expect((editor.element as HTMLTextAreaElement).value).toBe('local text')
    expect(state.dialog.warning).toHaveBeenCalledTimes(1)
    expect(state.dialog.warning.mock.calls[0][0].negativeText).toBe('保留本地编辑')
    state.dialog.warning.mock.calls[0][0].onPositiveClick()
    await nextTick()
    expect((editor.element as HTMLTextAreaElement).value).toBe('server text')
    expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
  })

  it('a late response from chapter 1 cannot load into chapter 2', async () => {
    const previous = deferred<any>()
    state.api.getChapter.mockReturnValueOnce(previous.promise).mockResolvedValueOnce(chapter('chapter two', 4, 2))
    const wrapper = wrap(ChapterView)
    await nextTick()
    state.route.params.id = '2'
    await flushPromises()
    expect((wrapper.find('textarea.content-editor').element as HTMLTextAreaElement).value).toBe('chapter two')
    previous.resolve(chapter('late chapter one', 1, 1))
    await flushPromises()
    expect((wrapper.find('textarea.content-editor').element as HTMLTextAreaElement).value).toBe('chapter two')
  })
})

describe('actual history and durable task component', () => {
  it('flushes local edits then restores using the acknowledged CAS token', async () => {
    const beforeRestore = vi.fn(async () => { await wrapper.setProps({ currentVersion: version(2) }); return true })
    const wrapper = wrap(HistoryModal, { show: true, slug: 'book', chapterNumber: 1, currentVersion: version(1), beforeRestore })
    state.api.restoreVersion.mockResolvedValue(chapter('old', 3))
    await flushPromises()
    await wrapper.find('select').setValue('0')
    await flushPromises()
    expect(wrapper.find('select').text()).toContain('旧章节名')
    expect(wrapper.text()).toContain('所选历史章节名：旧章节名')
    expect(wrapper.text()).toContain('历史章节名和正文一起保存')
    await button(wrapper, '恢复章节名与正文为新版本').trigger('click')
    await flushPromises()
    expect(beforeRestore).toHaveBeenCalledTimes(1)
    expect(state.api.restoreVersion).toHaveBeenCalledWith('book', 1, 0, version(2))
    expect(wrapper.emitted('restored')?.[0]).toEqual([chapter('old', 3)])
    expect(state.message.success).toHaveBeenCalledWith('历史章节名与正文已恢复为版本 3；章后分析已排队')
  })

  it('a failed local flush prevents historical restore and index rebuild', async () => {
    const wrapper = wrap(HistoryModal, { show: true, slug: 'book', chapterNumber: 1, currentVersion: version(1), beforeRestore: async () => false })
    await flushPromises()
    await wrapper.find('select').setValue('0')
    await flushPromises()
    await button(wrapper, '恢复章节名与正文为新版本').trigger('click')
    await button(wrapper, '重建正文与摘要索引').trigger('click')
    await flushPromises()
    expect(state.api.restoreVersion).not.toHaveBeenCalled()
    expect(state.api.requestDerivation).not.toHaveBeenCalled()
    expect(wrapper.emitted('restored')).toBeUndefined()
  })

  it('failed derivation remains visible and can be explicitly retried', async () => {
    const job = { job_id: 'job', revision: 1, status: 'unavailable', attempts: 2, last_error: 'evolution_snapshot: failed' }
    state.api.listDerivationJobs.mockResolvedValue([job])
    state.api.requestDerivation.mockResolvedValue({ ...job, status: 'pending' })
    const wrapper = wrap(HistoryModal, { show: true, slug: 'book', chapterNumber: 1, currentVersion: version(1) })
    await flushPromises()
    expect(wrapper.text()).toContain('失败，可重试')
    expect(wrapper.text()).toContain('evolution_snapshot: failed')
    await button(wrapper, '重试章后关联分析').trigger('click')
    await flushPromises()
    expect(state.api.requestDerivation).toHaveBeenCalledWith('book', 1, 'retry_aftermath')
    expect(wrapper.text()).toContain('已排队')
  })

  it('loads legacy drafts, previews their diff, and restores through the draft CAS endpoint', async () => {
    const draft = { id: 'draft-1', novel_id: 'book', chapter_id: 'c', chapter_number: 1, content: '旧草稿', outline: '', source: 'pre_regen', word_count: 3, created_at: '2026-10-12' }
    state.api.listDrafts.mockResolvedValue([draft])
    state.api.restoreDraft.mockResolvedValue(chapter('旧草稿', 2))
    const wrapper = wrap(HistoryModal, { show: true, slug: 'book', chapterNumber: 1, currentVersion: version(1) })
    await flushPromises()
    const selects = wrapper.findAll('select')
    expect(selects).toHaveLength(2)
    await selects[1].setValue('draft-1')
    await flushPromises()
    expect(state.api.diffDraft).toHaveBeenCalledWith('book', 1, 'draft-1', 1)
    expect(wrapper.text()).toContain('所选旧草稿未记录章节名，恢复时保留当前章节名。')
    expect(wrapper.text()).not.toContain('所选历史章节名：')
    await button(wrapper, '恢复此正文为新版本').trigger('click')
    await flushPromises()
    expect(state.api.restoreDraft).toHaveBeenCalledWith('book', 1, 'draft-1', version(1))
    expect(wrapper.emitted('restored')?.[0]).toEqual([chapter('旧草稿', 2)])
    expect(state.message.success).toHaveBeenCalledWith('旧草稿正文已恢复为版本 2；章后分析已排队')
  })
})
