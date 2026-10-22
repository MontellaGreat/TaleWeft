import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import { chapterApi, type ChapterDTO } from '../src/api/chapter'
import { apiClient } from '../src/api/config'
vi.mock('../src/api/config', () => ({ apiClient: { put: vi.fn(), get: vi.fn() }, subscribeChapterStream: vi.fn() }))
vi.stubGlobal('crypto', webcrypto)
const hash = (content: string) => createHash('sha256').update(content).digest('hex')
const saved = { id: 'chapter', novel_id: 'book', number: 1, title: 'Title', content: 'submitted', status: 'draft', word_count: 9,
  revision: 2, content_hash: hash('submitted'), etag: 'etag-2', save_request_id: 'request-1', created_at: 'today', updated_at: 'today' } satisfies ChapterDTO
const request = { content: 'submitted', save_request_id: 'request-1' }
const version = { revision: 1, content_hash: hash('base'), etag: 'etag-1' }
const receipt = { request_id: 'request-1', novel_id: 'book', chapter_number: 1, chapter_id: 'chapter', revision: 2,
  status: 'committed', content_hash: hash('submitted'), chapter: saved }
afterEach(() => vi.clearAllMocks())
describe('exact chapter save response and reconciliation', () => {
  it('accepts only the matching normal response with request identity, content hash and next revision', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(saved)
    expect(await chapterApi.updateChapter('book', 1, request, version)).toEqual(saved)
    expect(apiClient.get).not.toHaveBeenCalled()
  })
  it.each(['request', 'scope', 'chapter', 'revision', 'content', 'hash'])('does not accept a normal 200 with wrong %s', async mismatch => {
    const changes = { request: { save_request_id: 'other' }, scope: { novel_id: 'other' }, chapter: { number: 2 },
      revision: { revision: 4 }, content: { content: 'other' }, hash: { content_hash: 'wrong' } }[mismatch]!
    vi.mocked(apiClient.put).mockResolvedValue({ ...saved, ...changes })
    vi.mocked(apiClient.get).mockResolvedValue({ request_id: 'request-1', status: 'unconfirmed' })
    await expect(chapterApi.updateChapter('book', 1, request, version)).rejects.toThrow('尚未得到提交回执')
  })
  it('recovers the immutable committed version after a lost response', async () => {
    vi.mocked(apiClient.put).mockRejectedValue({ response: { status: 503 } })
    vi.mocked(apiClient.get).mockResolvedValue(receipt)
    expect(await chapterApi.updateChapter('book', 1, request, version)).toEqual(saved)
    expect(apiClient.get).toHaveBeenCalledWith('/novels/book/chapters/1/save-requests/request-1')
  })
  it.each(['failed', 'unconfirmed', 'unavailable', 'scope', 'chapter_id', 'hash'])('preserves an unresolved save when reconciliation has %s', async mismatch => {
    vi.mocked(apiClient.put).mockRejectedValue({ response: { status: 503 } })
    const changes = { failed: { status: 'failed' }, unconfirmed: { status: 'unconfirmed' }, unavailable: { status: 'unavailable' },
      scope: { novel_id: 'other' }, chapter_id: { chapter_id: 'other' }, hash: { content_hash: 'wrong' } }[mismatch]!
    vi.mocked(apiClient.get).mockResolvedValue({ ...receipt, ...changes })
    await expect(chapterApi.updateChapter('book', 1, request, version)).rejects.toThrow('本地正文已保留')
  })
  it('pins request content and version when the caller changes them during hashing or I/O', async () => {
    let resolve!: (value: ChapterDTO) => void
    vi.mocked(apiClient.put).mockImplementation(() => new Promise(done => { resolve = done }))
    const mutableRequest = { ...request }, mutableVersion = { ...version }
    const pending = chapterApi.updateChapter('book', 1, mutableRequest, mutableVersion)
    mutableRequest.content = 'later input'; mutableVersion.revision = 99
    await vi.waitFor(() => expect(apiClient.put).toHaveBeenCalled())
    resolve(saved)
    expect(await pending).toEqual(saved)
    expect(apiClient.put).toHaveBeenCalledWith('/novels/book/chapters/1', expect.objectContaining({ content: 'submitted', expected_revision: 1 }), { headers: { 'If-Match': 'etag-1' } })
  })
})
