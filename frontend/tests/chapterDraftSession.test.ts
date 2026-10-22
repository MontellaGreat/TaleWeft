import { afterEach, describe, expect, it } from 'vitest'
import { ChapterDraftSession, rememberChapterDraft, restoreChapterDraft } from '../src/workbench/chapterDraftSession'
import type { ChapterDTO } from '../src/api/chapter'

const version = { revision: 1, content_hash: 'base-hash', etag: 'base-etag' }
const saved = (content: string, revision: number) => ({ content, revision, content_hash: `hash-${revision}`, etag: `etag-${revision}` } as ChapterDTO)
afterEach(() => sessionStorage.clear())

describe('editor persistence races', () => {
  it('saves title edits and retains a newer rename made during commit', async () => {
    const editor = new ChapterDraftSession('novel', 1, 'base', { ...version, title: 'old title' })
    editor.title = 'submitted title'
    expect(editor.dirty).toBe(true)
    let acknowledge!: (value: ChapterDTO) => void
    const pending = editor.save((_slug, _chapter, body) => {
      expect(body.title).toBe('submitted title')
      return new Promise(resolve => { acknowledge = resolve })
    })
    editor.title = 'newer title'
    acknowledge({ ...saved('base', 2), title: 'submitted title' })
    await pending
    expect(editor.originalTitle).toBe('submitted title')
    expect(editor.title).toBe('newer title')
    expect(editor.dirty).toBe(true)
    rememberChapterDraft(editor)
    const restored = new ChapterDraftSession('novel', 1, 'base', { ...version, title: 'submitted title' })
    expect(restoreChapterDraft(restored)).toBe(true)
    expect(restored.title).toBe('newer title')
    expect(restored.originalTitle).toBe('submitted title')
  })
  it('keeps keystrokes typed while the writer is committing dirty', async () => {
    const editor = new ChapterDraftSession('novel', 1, 'base', version)
    editor.content = 'submitted'
    let acknowledge!: (value: ChapterDTO) => void
    const pending = editor.save(() => new Promise(resolve => { acknowledge = resolve }))
    editor.content = 'typed during commit'
    acknowledge(saved('submitted', 2))
    await pending
    expect(editor.content).toBe('typed during commit')
    expect(editor.originalContent).toBe('submitted')
    expect(editor.dirty).toBe(true)
    expect(editor.version.revision).toBe(2)
  })

  it('serializes autosave and route flush with the acknowledged revision', async () => {
    const editor = new ChapterDraftSession('novel', 1, 'base', version)
    editor.content = 'first'
    let acknowledge!: (value: ChapterDTO) => void
    const calls: number[] = []
    const first = editor.save((_slug, _chapter, _body, token) => {
      calls.push(token.revision)
      return new Promise(resolve => { acknowledge = resolve })
    })
    editor.content = 'second'
    const second = editor.save(async (_slug, _chapter, body, token) => {
      calls.push(token.revision)
      return saved(body.content!, 3)
    })
    expect(calls).toEqual([1])
    acknowledge(saved('first', 2))
    await Promise.all([first, second])
    expect(calls).toEqual([1, 2])
    expect(editor.dirty).toBe(false)
    expect(editor.version.revision).toBe(3)
  })

  it('preserves a conflicted draft through remount and keeps its old CAS token', async () => {
    const editor = new ChapterDraftSession('novel', 1, 'base', version)
    editor.content = 'local changes'
    await expect(editor.save(async () => { throw new Error('409') })).rejects.toThrow('409')
    rememberChapterDraft(editor)
    const remounted = new ChapterDraftSession('novel', 1, 'server changes', { ...version, revision: 5 })
    expect(restoreChapterDraft(remounted)).toBe(true)
    expect(remounted.content).toBe('local changes')
    expect(remounted.version.revision).toBe(1)
    expect(remounted.dirty).toBe(true)
    expect(restoreChapterDraft(new ChapterDraftSession('other', 1, 'base', version))).toBe(false)
  })
})
