import { readJsonSse } from './jsonSseReader'
import { apiClient, authenticatedFetch, resolveHttpUrl } from './config'
import { formatApiError } from '@/utils/apiError'

export interface LLMConfigProfile {
  id: string
  name: string
  provider: 'openai' | 'anthropic'
  api_key: string
  base_url: string
  model: string
  system_model: string
  writing_model: string
  created_at: string
  updated_at: string
}

export interface LLMConfigStore {
  active_id: string | null
  configs: LLMConfigProfile[]
}

export interface EmbeddingConfig {
  mode: 'local' | 'openai'
  api_key: string
  base_url: string
  model: string
  use_gpu: boolean
  model_path: string
}

export interface TracePrivacySettings {
  enabled: boolean
  capture_fulltext: boolean
  retention_days: number
  max_spans: number
}

export interface CredentialOwner {
  table: string
  record_id_sha256: string
  field: string
}

export interface CredentialReference {
  credential_ref: string
  ciphertext_sha256: string | null
  size_bytes: number | null
  created_at: string | null
  updated_at: string | null
  status: 'active' | 'orphaned' | 'missing'
  owners: CredentialOwner[]
}

// ── 扩展包安装相关类型 ──

export interface ExtensionsStatus {
  faiss: boolean
  numpy: boolean
  sentence_transformers: boolean
  all_installed: boolean
  install_running: boolean
  install_progress: string
  model_ready: boolean
  local_ready: boolean
  model_path: string
  download_source?: string | null
  downloaded_model_ready?: boolean
  downloaded_model_path?: string
  checking?: boolean
  dependency_error?: string
}

export interface InstallEvent {
  type: 'info' | 'success' | 'warn' | 'error' | 'progress' | 'log' | 'done'
  message: string
  percent?: number
  success?: boolean
  installed?: ExtensionsStatus
  phase?: string
  model_ready?: boolean
  model_path?: string
  download_source?: string
}

function tracePrivacyPath(suffix: string, novelId?: string) {
  return `/settings/trace-privacy${suffix}${novelId ? `?novel_id=${encodeURIComponent(novelId)}` : ''}`
}

export const settingsApi = {
  getTracePrivacy: (novelId?: string) => apiClient.get<TracePrivacySettings>(tracePrivacyPath('', novelId)),
  saveTracePrivacy: (data: TracePrivacySettings, novelId?: string) =>
    apiClient.put<TracePrivacySettings>(tracePrivacyPath('', novelId), data),
  resetTracePrivacy: (novelId: string) => apiClient.delete<TracePrivacySettings>(tracePrivacyPath('', novelId)),
  exportTraceRecords: (novelId?: string) => apiClient.get<Record<string, unknown>>(tracePrivacyPath('/records/export', novelId)),
  clearTraceRecords: (novelId?: string) =>
    apiClient.delete<{ deleted: Record<string, number> }>(tracePrivacyPath('/records', novelId)),
  listCredentials: () => apiClient.get<{ format: string; credentials: CredentialReference[] }>('/settings/credentials'),
  exportCredentials: () => apiClient.get<{ format: string; exported_at: string; warning: string; credentials: CredentialReference[] }>('/settings/credentials/export'),
  deleteCredential: (credentialRef: string) => apiClient.delete<{ credential_ref: string; deleted: boolean }>(`/settings/credentials/${encodeURIComponent(credentialRef)}`),
  listLLMConfigs: () =>
    apiClient.get<LLMConfigStore>('/settings/llm-configs'),

  createLLMConfig: (data: Pick<LLMConfigProfile, 'name' | 'provider' | 'api_key' | 'base_url' | 'model'>) =>
    apiClient.post<LLMConfigProfile>('/settings/llm-configs', data),

  updateLLMConfig: (id: string, data: Partial<LLMConfigProfile>) =>
    apiClient.put<LLMConfigProfile>(`/settings/llm-configs/${id}`, data),

  deleteLLMConfig: (id: string) =>
    apiClient.delete<void>(`/settings/llm-configs/${id}`),

  activateLLMConfig: (id: string) =>
    apiClient.post<void>(`/settings/llm-configs/${id}/activate`),

  fetchModels: (data: { provider: string; api_key: string; base_url: string; profile_id?: string }) =>
    apiClient.post<string[]>('/settings/llm-configs/fetch-models', data),

  getEmbeddingConfig: () =>
    apiClient.get<EmbeddingConfig>('/settings/embedding'),

  updateEmbeddingConfig: (data: EmbeddingConfig) =>
    apiClient.put<EmbeddingConfig>('/settings/embedding', data),

  fetchEmbeddingModels: (data: { provider: string; api_key: string; base_url: string }) =>
    apiClient.post<string[]>('/settings/embedding/fetch-models', data),

  // ── 扩展包安装（本地 AI 引擎）──

  /** 检查本地 AI 扩展包安装状态 */
  getExtensionsStatus: () =>
    apiClient.get<ExtensionsStatus>('/system/extensions-status'),

  /**
   * 安装本地 AI 扩展包（SSE 流式）
   * 返回 AbortController 用于取消
   */
  installExtensions: (handlers: {
    onEvent?: (event: InstallEvent) => void
    onDone?: (success: boolean) => void
    onError?: (error: Error) => void
  }): AbortController => {
    const ctrl = new AbortController()

    void (async () => {
      try {
        const url = resolveHttpUrl('/api/v1/system/install-extensions')
        const res = await authenticatedFetch(url, {
          method: 'POST',
          signal: ctrl.signal,
          headers: {
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
        })

        if (!res.ok || !res.body || res.headers.get('content-type')?.includes('application/json')) {
          const body: unknown = await res.json().catch(() => null)
          const err = new Error(formatApiError({ response: { data: body } }, `安装服务返回 HTTP ${res.status}，未启动下载。`))
          handlers.onError?.(err)
          return
        }

        let terminalSeen = false
        await readJsonSse(res, raw => {
          const event = raw as unknown as InstallEvent
          handlers.onEvent?.(event)
          if (event.type === 'done') {
            terminalSeen = true
            handlers.onDone?.(event.success ?? false)
          }
          return terminalSeen
        }, ctrl.signal)
        if (!terminalSeen && !ctrl.signal.aborted) handlers.onError?.(new Error('连接中断，未收到安装完成确认'))
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return
        handlers.onError?.(e instanceof Error ? e : new Error('Stream error'))
      }
    })()

    return ctrl
  },
}
