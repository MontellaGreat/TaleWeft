import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// Execute the production coordinator, rather than copying its implementation.
const source = await readFile(new URL('../src/workbench/chapterDraftSession.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } })
const { ChapterDraftSession, rememberChapterDraft, restoreChapterDraft } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const version = (revision) => ({ revision, content_hash: `hash-${revision}`, etag: `"revision-${revision}"` })
const response = (content, revision) => ({ content, ...version(revision) })
const deferred = () => {
  let resolve, reject
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

test('concurrent saves serialize, use committed revision, and retain keystrokes after submission', async () => {
  const session = new ChapterDraftSession('book', 1, 'baseline', version(0))
  const calls = []
  const first = deferred(), second = deferred()
  const save = (_slug, _number, data, token) => {
    calls.push({ data, token })
    return calls.length === 1 ? first.promise : second.promise
  }
  session.content = 'first edit'
  const savingFirst = session.save(save)
  session.content = 'second edit'
  const flush = session.save(save)
  assert.equal(calls.length, 1)
  first.resolve(response('first edit', 1))
  await savingFirst
  assert.equal(session.content, 'second edit')
  assert.equal(session.dirty, true)
  assert.equal(calls[1].token.revision, 1)
  assert.equal(calls[1].data.content, 'second edit')
  session.content = 'typed during flush'
  second.resolve(response('second edit', 2))
  await flush
  assert.equal(session.version.revision, 2)
  assert.equal(session.originalContent, 'second edit')
  assert.equal(session.content, 'typed during flush')
  assert.equal(session.dirty, true)
  await session.save(async (_slug, _number, data, token) => response(data.content, token.revision + 1))
  assert.equal(session.dirty, false)
})

test('failed or conflicting commits never advance version or discard local content', async () => {
  const session = new ChapterDraftSession('book', 1, 'baseline', version(3))
  session.content = 'local text'
  const failure = deferred()
  const first = session.save(() => failure.promise)
  const queued = session.save(() => { throw new Error('must not run after rejection') })
  failure.reject({ response: { status: 409 } })
  const result = await Promise.allSettled([first, queued])
  assert.equal(result.filter(item => item.status === 'rejected').length, 2)
  assert.deepEqual(session.version, version(3))
  assert.equal(session.originalContent, 'baseline')
  assert.equal(session.content, 'local text')
  assert.equal(session.dirty, true)
})

test('late ACK stays attached to the old chapter and cannot alter the next document', async () => {
  const old = new ChapterDraftSession('old-book', 1, 'old', version(0))
  const next = new ChapterDraftSession('new-book', 2, 'new', version(6))
  const ack = deferred()
  old.content = 'old edit'
  const saved = old.save((slug, number) => {
    assert.equal(slug, 'old-book')
    assert.equal(number, 1)
    return ack.promise
  })
  ack.resolve(response('old edit', 1))
  await saved
  assert.equal(next.content, 'new')
  assert.equal(next.originalContent, 'new')
  assert.deepEqual(next.version, version(6))
})

test('local recovery retains the original CAS baseline, and committed text clears the recovery draft', () => {
  const storage = new Map()
  globalThis.sessionStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  }
  const local = new ChapterDraftSession('book', 1, 'baseline', version(1))
  local.content = 'uncommitted local'
  rememberChapterDraft(local)
  const reopened = new ChapterDraftSession('book', 1, 'server changed', version(2))
  assert.equal(restoreChapterDraft(reopened), true)
  assert.equal(reopened.content, 'uncommitted local')
  assert.equal(reopened.originalContent, 'baseline')
  assert.equal(reopened.version.revision, 1)
  const committed = new ChapterDraftSession('book', 1, 'uncommitted local', version(2))
  assert.equal(restoreChapterDraft(committed), false)
  assert.equal(storage.size, 0)
})
