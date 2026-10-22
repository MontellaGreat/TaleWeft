import { apiClient } from './config'

export type PluginPermission = 'prompts' | 'quality_rules'
export interface TrustedPluginKey {
  id: string; name: string; public_key: string; fingerprint: string; permissions: PluginPermission[]
}
export interface PluginManifest {
  schema_version: 1; id: string; name: string; version: string; signer_id: string; permissions: PluginPermission[]
}
export interface PluginPackage {
  manifest: PluginManifest
  payload: { prompts: Array<Record<string, unknown>>; quality_rules: LiteralQualityRule[] }
  signature: string
}
export interface InstalledPlugin extends PluginManifest { digest: string; created_at: string; prompt_count: number; rule_count: number }
export interface PluginPreview { id: string; name: string; version: string; signer_id: string; permissions: PluginPermission[]; fingerprint: string; digest: string; prompt_count: number; rule_count: number; verified: boolean }
export interface LiteralQualityRule { name: string; text: string; severity: 'critical' | 'warning' | 'info'; category: string; replacement_hint: string }
export interface QualityRuleConfig { disabled_builtin: string[]; rules: LiteralQualityRule[] }
export interface QualityRuleVersion { id: string; config: QualityRuleConfig; note: string; source: string; created_at: string }
export interface EffectiveRules { version: string; policy_version: string; patterns: string[][]; plugins: Array<{ id: string; digest: string }> }
export interface QualityRulesOverview { current: QualityRuleVersion; versions: QualityRuleVersion[]; effective: EffectiveRules; builtin: Array<{ name: string; severity: string; category: string; replacement_hint: string }> }
export interface QualityScanPreview { rule_version: string; content_hash: string; metrics: { total_hits: number; critical_hits: number; severity_score: number; overall_assessment: string }; hits: Array<{ pattern: string; text: string; replacement_hint: string }> }

export const extensionPolicyApi = {
  plugins: () => apiClient.get<{ keys: TrustedPluginKey[]; packages: InstalledPlugin[] }>('/extensions/plugins'),
  trust: (key: Pick<TrustedPluginKey, 'id' | 'name' | 'public_key' | 'permissions'>) => apiClient.post<TrustedPluginKey>('/extensions/plugins/keys', key),
  revoke: (id: string) => apiClient.delete(`/extensions/plugins/keys/${encodeURIComponent(id)}`),
  verify: (data: PluginPackage) => apiClient.post<PluginPreview>('/extensions/plugins/verify', data),
  install: (data: PluginPackage, digest: string) => apiClient.post<PluginPreview>('/extensions/plugins/install', { package: data, expected_digest: digest }),
  uninstall: (id: string) => apiClient.delete(`/extensions/plugins/${encodeURIComponent(id)}`),
  rules: () => apiClient.get<QualityRulesOverview>('/extensions/quality-rules'),
  publish: (config: QualityRuleConfig, expectedVersion: string, note: string) => apiClient.post<QualityRuleVersion>('/extensions/quality-rules', { config, expected_version: expectedVersion, note }),
  rollback: (version: string, expectedVersion: string) => apiClient.post<QualityRuleVersion>(`/extensions/quality-rules/versions/${encodeURIComponent(version)}/rollback`, { expected_version: expectedVersion }),
  scan: (content: string) => apiClient.post<QualityScanPreview>('/extensions/quality-rules/scan', { content }),
}

export function extensionError(error: unknown): string {
  if (error && typeof error === 'object') {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return error instanceof Error ? error.message : '操作失败，请重试'
}
