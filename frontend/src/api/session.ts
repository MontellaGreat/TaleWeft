/** Session capabilities are supplied by a launcher fragment or trusted Tauri IPC. */
let sessionToken = ''
let browserPrompted = false
const storageKey = 'plotpilot.local-session'

export function setSessionToken(token: string, persist = false): void {
  if (token.length < 32 || /\s/.test(token)) throw new Error('本地会话密钥格式无效')
  sessionToken = token
  if (persist) {
    try { sessionStorage.setItem(storageKey, token) } catch { /* Memory-only if storage is blocked. */ }
  }
}

export function currentSessionToken(): string { return sessionToken }

export function loadBrowserSession(): void {
  if (typeof window === 'undefined' || sessionToken) return
  const url = new URL(window.location.href)
  const fragment = new URLSearchParams(url.hash.slice(1))
  const launchedToken = fragment.get('plotpilot-session')
  if (launchedToken) {
    // Remove before any other navigation, telemetry, or request is allowed.
    fragment.delete('plotpilot-session')
    url.hash = fragment.toString()
    window.history.replaceState(window.history.state, '', url)
    setSessionToken(launchedToken, true)
    return
  }
  let stored: string | null = null
  try { stored = sessionStorage.getItem(storageKey) } catch { /* Optional storage. */ }
  if (stored) setSessionToken(stored)
}

export function requireBrowserSession(): void {
  loadBrowserSession()
  if (sessionToken) return
  if (!browserPrompted) {
    browserPrompted = true
    const token = window.prompt('请输入安全启动器提供的本地会话密钥，或使用启动器生成的链接打开软件。')
    if (token?.trim()) setSessionToken(token.trim(), true)
  }
  if (!sessionToken) throw new Error('缺少本地会话。请使用 scripts/run_local_secure.py 启动软件。')
}
