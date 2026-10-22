import { expect, it, vi } from 'vitest'
import { novelApi } from '../src/api/novel'

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }))
vi.mock('../src/api/config', () => ({ apiClient: api }))

it('novel collection requests use the canonical route without a WebView POST redirect', async () => {
  const data = { novel_id: 'book', title: '测试', author: '', target_chapters: 100 }
  api.post.mockResolvedValue({ id: 'book' })
  api.get.mockResolvedValue([])
  await novelApi.createNovel(data)
  await novelApi.listNovels()
  expect(api.post).toHaveBeenCalledWith('/novels/', data)
  expect(api.get.mock.calls[0]?.[0]).toBe('/novels/')
})
