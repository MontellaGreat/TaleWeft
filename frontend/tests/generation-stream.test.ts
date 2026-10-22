import { beforeEach, describe, expect, it, vi } from 'vitest'
import { consumeGenerateChapterStream, consumeHostedWriteStream } from '../src/api/workflow'

const transport = vi.hoisted(() => ({ fetch: vi.fn(), post: vi.fn() }))
vi.mock('../src/api/config', () => ({
  authenticatedFetch: transport.fetch, resolveHttpUrl: (url: string) => url,
  apiClient: { post: transport.post },
}))

const request = { chapter_number: 1, outline: 'outline', request_id: 'testrequest' }
const event = (sequence: number, type: string, extra: object = {}) => ({ request_id: request.request_id, sequence, type, ...extra })
const response = (events: object[], fragmentBytes = 4096, finalSeparator = true) => {
  const text = events.map(row => 'data:' + JSON.stringify(row)).join('\r\n\r\n') + (finalSeparator ? '\r\n\r\n' : '')
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += fragmentBytes) controller.enqueue(bytes.slice(offset, offset + fragmentBytes))
    controller.close()
  } }))
}

beforeEach(() => {
  transport.fetch.mockReset()
  transport.post.mockReset().mockResolvedValue({ status: 'cancelled' })
})

describe('production hosted SSE consumer', () => {
  const body = { from_chapter: 1, to_chapter: 2, auto_save: true, auto_outline: true, request_id: request.request_id }
  it('replays CRLF and split UTF-8 with one session completion confirmed by terminal', async () => {
    transport.fetch.mockResolvedValueOnce(response([event(1, 'chunk', { text: '中文' })], 1))
      .mockResolvedValueOnce(response([event(1, 'chunk', { text: '中文' }), event(2, 'saved', { chapter: 1, revision: 2 }),
        event(3, 'session_done', { session_id: 'hosted' }), event(4, 'terminal', { status: 'completed' })], 1, false))
    const seen: Record<string, unknown>[] = []
    const terminal = vi.fn()
    await consumeHostedWriteStream('book', body, { onEvent: row => seen.push(row), onTerminal: terminal })
    expect(seen.filter(row => row.type === 'chunk')).toHaveLength(1)
    expect(seen.filter(row => row.type === 'session_done')).toHaveLength(1)
    expect(JSON.parse(transport.fetch.mock.calls[1][1].body).resume_after).toBe(1)
    expect(terminal).toHaveBeenCalledWith('completed', undefined)
  })
  it('EOF after session_done cannot claim completed without the durable terminal', async () => {
    transport.fetch.mockImplementation(async () => response([event(1, 'session_done')]))
    const eventCallback = vi.fn(), error = vi.fn()
    await consumeHostedWriteStream('book', body, { onEvent: eventCallback, onError: error })
    expect(eventCallback.mock.calls.some(call => call[0].type === 'session_done')).toBe(false)
    expect(error).toHaveBeenCalledTimes(1)
    expect(transport.fetch).toHaveBeenCalledTimes(3)
  })
  it('resume after session_done hydrates its terminal result', async () => {
    transport.fetch.mockResolvedValue(response([event(4, 'terminal', { status: 'completed', result: event(3, 'session_done', { session_id: 'hosted' }) })]))
    const seen = vi.fn()
    await consumeHostedWriteStream('book', { ...body, resume_after: 3 }, { onEvent: seen })
    expect(seen).toHaveBeenCalledWith(expect.objectContaining({ type: 'session_done', session_id: 'hosted' }))
  })
})

describe('production chapter SSE consumer', () => {
  it('decodes split UTF-8 and CRLF, accepts the last frame without a blank separator, and only completes at terminal', async () => {
    transport.fetch.mockResolvedValue(response([
      event(1, 'request_started'), event(2, 'chunk', { text: '中文正文' }),
      event(3, 'done', { content: '中文正文', token_count: 4 }), event(4, 'terminal', { status: 'completed' }),
    ], 1, false))
    const order: string[] = []
    const onDone = vi.fn(() => order.push('done'))
    await consumeGenerateChapterStream('book', request, {
      onChunk: text => order.push(text), onDone, onTerminal: status => order.push(status),
    })
    expect(order).toEqual(['中文正文', 'done', 'completed'])
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(transport.fetch).toHaveBeenCalledTimes(1)
  })

  it('reconnects with the same idempotency token and cursor and never repeats chunks', async () => {
    transport.fetch.mockResolvedValueOnce(response([event(1, 'request_started'), event(2, 'chunk', { text: 'first' })]))
      .mockResolvedValueOnce(response([event(2, 'chunk', { text: 'first' }), event(3, 'chunk', { text: 'second' }),
        event(4, 'done', { content: 'firstsecond' }), event(5, 'terminal', { status: 'completed' })]))
    const chunks: string[] = []
    const done = vi.fn(), reconnect = vi.fn()
    await consumeGenerateChapterStream('book', request, { onChunk: text => chunks.push(text), onDone: done, onReconnect: reconnect })
    expect(chunks).toEqual(['first', 'second'])
    expect(done).toHaveBeenCalledTimes(1)
    const bodies = transport.fetch.mock.calls.map(call => JSON.parse(call[1].body))
    expect(bodies.map(body => body.resume_after)).toEqual([0, 2])
    expect(new Set(bodies.map(body => body.request_id)).size).toBe(1)
    expect(reconnect).toHaveBeenCalledWith(1)
  })

  it('EOF without completion remains a failure after bounded reconnects', async () => {
    transport.fetch.mockImplementation(async () => response([event(1, 'chunk', { text: 'partial' })]))
    const done = vi.fn(), failed = vi.fn(), chunks = vi.fn()
    await consumeGenerateChapterStream('book', request, { onDone: done, onError: failed, onChunk: chunks })
    expect(done).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledTimes(1)
    expect(chunks).toHaveBeenCalledTimes(1)
    expect(transport.fetch).toHaveBeenCalledTimes(3)
  })

  it('cancellation preserves partial output and sends a backend cancel request', async () => {
    const ctrl = new AbortController()
    transport.fetch.mockImplementation(async (_url, options) => {
      ctrl.abort()
      throw new DOMException('aborted', 'AbortError')
    })
    const done = vi.fn(), error = vi.fn()
    await consumeGenerateChapterStream('book', request, { signal: ctrl.signal, onDone: done, onError: error })
    expect(transport.post).toHaveBeenCalledWith('/novels/book/generation-requests/testrequest/cancel', {})
    expect(done).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })

  it('completed terminal hydrates the result when resume starts after done', async () => {
    transport.fetch.mockResolvedValue(response([event(4, 'terminal', { status: 'completed', result: { type: 'done', content: 'whole' } })]))
    const done = vi.fn()
    await consumeGenerateChapterStream('book', { ...request, resume_after: 3 }, { onDone: done })
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ content: 'whole' }))
  })

  it('cancelled and interrupted terminals never mark a chapter generated', async () => {
    for (const status of ['cancelled', 'interrupted']) {
      transport.fetch.mockResolvedValue(response([event(1, 'chunk', { text: 'partial' }), event(2, 'terminal', { status })]))
      const done = vi.fn(), terminal = vi.fn()
      await consumeGenerateChapterStream('book', request, { onDone: done, onTerminal: terminal })
      expect(done).not.toHaveBeenCalled()
      expect(terminal).toHaveBeenCalledWith(status, undefined)
    }
  })

  it('events from another request cannot advance the cursor or replace this chapter', async () => {
    transport.fetch.mockResolvedValue(response([
      { request_id: 'oldrequest', sequence: 100, type: 'done', content: 'wrong' },
      event(1, 'done', { content: 'correct' }), event(2, 'terminal', { status: 'completed' }),
    ]))
    const done = vi.fn()
    await consumeGenerateChapterStream('book', request, { onDone: done })
    expect(done).toHaveBeenCalledTimes(1)
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ content: 'correct' }))
  })

  it('review-required opens its invocation exactly once and is distinct from completed generation', async () => {
    transport.fetch.mockResolvedValue(response([
      event(1, 'approval_required', { session_id: 'review', status: 'awaiting_pre_call_review' }),
      event(2, 'terminal', { status: 'awaiting_review' }),
    ]))
    const review = vi.fn(), done = vi.fn(), error = vi.fn()
    await consumeGenerateChapterStream('book', request, { onApprovalRequired: review, onDone: done, onError: error })
    expect(review).toHaveBeenCalledWith('review', 'awaiting_pre_call_review', undefined)
    expect(done).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })
})
