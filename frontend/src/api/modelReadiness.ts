import { apiClient } from './config'

export interface ModelReadiness {
  ready: boolean
  llm: { ready: boolean; reasons: string[] }
  embedding: { ready: boolean; reasons: string[]; mode: 'local' | 'openai' }
}

/** Checks saved configuration and local resources without running a generation. */
export const modelReadinessApi = {
  get: () => apiClient.get<ModelReadiness>('/settings/model-readiness'),
}
