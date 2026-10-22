import { authenticatedFetch, resolveHttpUrl } from './config'

/** EventSource facade: renew a single-use, 30-second ticket on every connection. */
class SessionEventSource extends EventTarget {
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSED = 2
  readonly withCredentials = false
  readonly url: string
  readyState = 0
  onopen: EventSource['onopen'] = null
  onerror: EventSource['onerror'] = null
  onmessage: EventSource['onmessage'] = null
  private source: EventSource | null = null
  private closed = false
  private retry: ReturnType<typeof setTimeout> | null = null
  private controller = new AbortController()
  private names = new Set<string>()

  constructor(url: string) {
    super()
    this.url = url
    void this.open()
  }

  override addEventListener(type: string, callback: EventListenerOrEventListenerObject | null,
    options?: boolean | AddEventListenerOptions): void {
    super.addEventListener(type, callback, options)
    if (!['open', 'error', 'message'].includes(type) && !this.names.has(type)) {
      this.names.add(type)
      this.source?.addEventListener(type, event => this.emit(type, event))
    }
  }

  close(): void {
    this.closed = true
    this.readyState = this.CLOSED
    this.controller.abort()
    this.source?.close()
    this.source = null
    if (this.retry) clearTimeout(this.retry)
    this.retry = null
  }

  private emit(type: string, original?: Event): void {
    const event = original instanceof MessageEvent
      ? new MessageEvent(type, { data: original.data, lastEventId: original.lastEventId, origin: original.origin })
      : new Event(type)
    const callback = type === 'open' ? this.onopen : type === 'error' ? this.onerror : this.onmessage
    if (['open', 'error', 'message'].includes(type)) callback?.call(this as unknown as EventSource, event as MessageEvent)
    this.dispatchEvent(event)
  }

  private failed(): void {
    if (this.closed) return
    this.source?.close()
    this.source = null
    this.readyState = this.CONNECTING
    this.emit('error')
    if (!this.closed) this.retry = setTimeout(() => { this.retry = null; void this.open() }, 1500)
  }

  private async open(): Promise<void> {
    if (this.closed) return
    try {
      const target = new URL(this.url, window.location.href)
      const response = await authenticatedFetch('/internal/event-ticket', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: target.pathname + target.search }), signal: this.controller.signal,
      })
      if (!response.ok) throw new Error(`Event authorization failed (${response.status})`)
      const result = await response.json() as { ticket: string }
      if (this.closed) return
      // Re-resolve after desktop IPC selected the active backend port.
      const streamUrl = new URL(resolveHttpUrl(target.pathname + target.search), window.location.href)
      streamUrl.searchParams.set('_session_ticket', result.ticket)
      const source = new EventSource(streamUrl.href)
      this.source = source
      source.onopen = event => { if (!this.closed) { this.readyState = this.OPEN; this.emit('open', event) } }
      source.onmessage = event => { if (!this.closed) this.emit('message', event) }
      source.onerror = () => this.failed()
      for (const name of this.names) source.addEventListener(name, event => this.emit(name, event))
    } catch {
      if (!this.closed) this.failed()
    }
  }
}

export function createAuthenticatedEventSource(url: string): EventSource {
  return new SessionEventSource(url) as unknown as EventSource
}
