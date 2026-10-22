// Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
declare global {
  interface Window {
    $message?: {
      success: (content: string) => void
      error: (content: string) => void
      warning: (content: string) => void
      info: (content: string) => void
    }
    PlotPilotFeedback?: Window['TaleWeftFeedback']
    TaleWeftFeedback?: {
      /** 控制台：触发与 UI 同构的事故快照 */
      reportError: (summary: string, err?: unknown) => void
      peekRecentIncidents: () => Array<{
        summary: string
        occurred_at: string
        detail_length: number
      }>
      exportRecentBundle: () => void
    }
  }
}

export {}
