import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readJsonSse } from '../src/api/jsonSseReader'
import { consumeMainPlotOptionsStream, consumePlotOutlineStream } from '../src/api/workflow'
import { consumeBibleGenerateStream } from '../src/api/bible'
import { streamMacroPlan, streamActChapterPlan, watchMacroPlanProgress } from '../src/api/planning'
import { settingsApi } from '../src/api/settings'

const transport = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('../src/api/config', () => ({ authenticatedFetch: transport.fetch, resolveHttpUrl: (url: string) => url, apiClient: {} }))
const response = (text: string) => {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte))
    controller.close()
  } }))
}
const named = (event: string, payload: object) => `event: ${event}\r\ndata:${JSON.stringify(payload)}\r\n\r\n`
const json = (payload: object) => `data:${JSON.stringify(payload)}\r\n\r\n`
const doneHandlers = () => {
  let resolve!: () => void
  const promise = new Promise<void>(ok => { resolve = ok })
  return { promise, done: vi.fn(resolve), error: vi.fn(resolve) }
}
beforeEach(() => transport.fetch.mockReset())

describe('shared SSE frame contract', () => {
  it('reads named multiline JSON, split UTF-8, CR-only frames, comments and an EOF frame', async () => {
    const events: unknown[] = []
    await readJsonSse(response(': comment\r\revent: chunk\rdata: {"text":\rdata: "中文"}\r\revent: done\rdata:{"status":"ok"}'),
      async (data, type) => { events.push([type, data]); return type === 'done' })
    expect(events).toEqual([['chunk', { text: '中文' }], ['done', { status: 'ok' }]])
  })
  it('returns a parser failure for malformed JSON and always cancels the reader', async () => {
    await expect(readJsonSse(response('data:broken\r\n\r\n'), () => false)).rejects.toThrow()
  })
})

describe('production named and wizard stream consumers', () => {
  it('setup streams accept CRLF and report missing terminal as failure', async () => {
    for (const consumer of [consumeMainPlotOptionsStream, consumePlotOutlineStream]) {
      transport.fetch.mockResolvedValueOnce(response(json({ type: 'phase', message: '中文' }) + json({ type: 'done', plot_options: [], plot_outline: {} })))
      const onDone = vi.fn(), onError = vi.fn()
      await consumer('book', { onDone, onError })
      expect(onDone).toHaveBeenCalledTimes(1)
      expect(onError).not.toHaveBeenCalled()
      transport.fetch.mockResolvedValueOnce(response(json({ type: 'phase', message: '中文' })))
      await consumer('book', { onDone, onError })
      expect(onError).toHaveBeenCalledTimes(1)
    }
  })
  it('Bible named data preserves nested payload type and requires a terminal', async () => {
    transport.fetch.mockResolvedValueOnce(response(named('data', { type: 'style_chunk', chunk: '风格' }) + named('done', { novel_id: 'book' })))
    const chunks = vi.fn(), done = vi.fn(), error = vi.fn()
    await consumeBibleGenerateStream('book', 'style', { onStyleChunk: chunks, onDone: done, onError: error })
    expect(chunks).toHaveBeenCalledWith('风格')
    expect(done).toHaveBeenCalledWith('book')
    expect(error).not.toHaveBeenCalled()
    transport.fetch.mockResolvedValueOnce(response(named('phase', { phase: 'writing' })))
    await consumeBibleGenerateStream('book', 'style', { onError: error })
    expect(error).toHaveBeenCalledTimes(1)
  })
  it('Bible request failures and unexpected aborts reach the caller, while user cancellation stays silent', async () => {
    const onError = vi.fn()
    transport.fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await consumeBibleGenerateStream('book', 'locations', { onError })
    expect(onError).toHaveBeenLastCalledWith('Failed to fetch')
    transport.fetch.mockRejectedValueOnce(new DOMException('Request aborted', 'AbortError'))
    await consumeBibleGenerateStream('book', 'locations', { onError })
    expect(onError).toHaveBeenLastCalledWith('Request aborted')
    const controller = new AbortController()
    transport.fetch.mockImplementationOnce(async () => { controller.abort(); throw new DOMException('Cancelled', 'AbortError') })
    await consumeBibleGenerateStream('book', 'locations', { onError, signal: controller.signal })
    expect(onError).toHaveBeenCalledTimes(2)
  })
  it.each(['', ' ', 123])('Bible rejects an invalid approval session (%s) instead of hanging', async session_id => {
    transport.fetch.mockResolvedValueOnce(response(named('data', { type: 'approval_required', session_id })))
    const onError = vi.fn(), onApprovalRequired = vi.fn()
    await consumeBibleGenerateStream('book', 'locations', { onError, onApprovalRequired })
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('会话编号'))
    expect(onApprovalRequired).not.toHaveBeenCalled()
  })
  it('macro and act planning distinguish completion from EOF after partial output', async () => {
    for (const consumer of [streamMacroPlan, streamActChapterPlan]) {
      transport.fetch.mockResolvedValueOnce(response(named('chunk', { text: '计划' }) + named('done', { structure: [] })))
      const success = doneHandlers()
      consumer('book', { onDone: success.done, onError: success.error })
      await success.promise
      expect(success.done).toHaveBeenCalledTimes(1)
      expect(success.error).not.toHaveBeenCalled()
      transport.fetch.mockResolvedValueOnce(response(named('chunk', { text: '计划' })))
      const failure = doneHandlers()
      consumer('book', { onDone: failure.done, onError: failure.error })
      await failure.promise
      expect(failure.done).not.toHaveBeenCalled()
      expect(failure.error).toHaveBeenCalledTimes(1)
    }
  })
  it('macro progress ends on authoritative terminal and EOF cannot impersonate it', async () => {
    transport.fetch.mockResolvedValueOnce(response(named('terminal', { status: 'completed' })))
    const terminal = doneHandlers()
    watchMacroPlanProgress('book', { onTerminal: terminal.done, onError: terminal.error })
    await terminal.promise
    expect(terminal.done).toHaveBeenCalledWith({ status: 'completed', message: undefined })
    transport.fetch.mockResolvedValueOnce(response(named('heartbeat', { tick: 1 })))
    const failure = doneHandlers()
    watchMacroPlanProgress('book', { onTerminal: failure.done, onError: failure.error })
    await failure.promise
    expect(failure.error).toHaveBeenCalledTimes(1)
  })
  it('extension install reports a terminated success and incomplete EOF separately', async () => {
    transport.fetch.mockResolvedValueOnce(response(json({ type: 'done', success: true })))
    const success = doneHandlers()
    settingsApi.installExtensions({ onDone: success.done, onError: success.error })
    await success.promise
    expect(success.done).toHaveBeenCalledWith(true)
    transport.fetch.mockResolvedValueOnce(response(json({ type: 'progress', message: 'partial' })))
    const failure = doneHandlers()
    settingsApi.installExtensions({ onDone: failure.done, onError: failure.error })
    await failure.promise
    expect(failure.done).not.toHaveBeenCalled()
    expect(failure.error).toHaveBeenCalledTimes(1)
  })
})
