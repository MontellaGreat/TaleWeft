import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { subscribeChapterStream } from '../src/api/config'
import { setSessionToken } from '../src/api/session'

vi.mock('../src/support/feedbackNotifier', () => ({ emitAxiosFeedbackIncident: vi.fn() }))
const response = (events: object[]) => new Response(new ReadableStream({ start(controller) {
  const bytes = new TextEncoder().encode(events.map(event => `data:${JSON.stringify(event)}`).join('\r\n\r\n') + '\r\n\r\n')
  for (const byte of bytes) controller.enqueue(Uint8Array.of(byte))
  controller.close()
} }))
beforeEach(() => setSessionToken('t'.repeat(64)))
afterEach(() => vi.unstubAllGlobals())

describe('authenticated production chapter subscription', () => {
  it('decodes split UTF-8/CRLF and reports only the backend stop terminal', async () => {
    const fetch = vi.fn().mockResolvedValue(response([
      { type: 'chapter_content', metadata: { chapter_number: 1, content: '中文', word_count: 2 } },
      { type: 'autopilot_stopped', message: 'stopped' },
    ]))
    vi.stubGlobal('fetch', fetch)
    const content = vi.fn(), error = vi.fn(), disconnected = vi.fn()
    await new Promise<void>(resolve => subscribeChapterStream('book', { onChapterContent: content, onError: error,
      onDisconnected: disconnected, onStreamEnd: reason => { expect(reason).toBe('stopped'); resolve() } }))
    expect(content).toHaveBeenCalledWith(expect.objectContaining({ content: '中文' }))
    expect(error).not.toHaveBeenCalled()
    expect(disconnected).not.toHaveBeenCalled()
    expect(fetch.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer ' + 't'.repeat(64))
  })
  it('EOF without a terminal is disconnected and cannot become idle/stopped', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response([{ type: 'connected' }])))
    const ended = vi.fn(), error = vi.fn()
    await new Promise<void>(resolve => subscribeChapterStream('book', { onStreamEnd: ended, onError: error, onDisconnected: resolve }))
    expect(ended).not.toHaveBeenCalled()
    expect(error).toHaveBeenCalledTimes(1)
  })
})
