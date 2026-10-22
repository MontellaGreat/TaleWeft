/**
 * Anti-AI 防御系统 — API 接口
 */
import type {
  ScanResult,
  PromptCategory,
  AntiAIRule,
  AllowlistScene,
  AllowlistUpdateRequest,
  AntiAIStats,
  AntiAIAuditRun,
  AntiAITrendPoint,
  AntiAIStreamRun,
  AntiAIPromptRun,
  AntiAIScanProgress,
  AntiAIRewritePolicy,
  RewriteCandidate,
  CandidateConfirmTokens,
} from '../types/anti-ai'
import { fetchJson, type FetchJsonOptions } from './http'

const API_BASE = '/api/v1/anti-ai'

function request<T>(path: string, options?: FetchJsonOptions): Promise<T> {
  return fetchJson<T>(`${API_BASE}${path}`, options)
}

/** 扫描章节 AI 味 */
export function scanChapter(content: string, chapterId?: string): Promise<ScanResult> {
  return request<ScanResult>('/scan', {
    method: 'POST',
    body: { content, chapter_id: chapterId || '' },
  })
}

/** 获取提示词分类信息 */
export function getCategories(): Promise<PromptCategory[]> {
  return request<PromptCategory[]>('/categories')
}

/** 获取正向行为映射规则 */
export function getRules(): Promise<AntiAIRule[]> {
  return request<AntiAIRule[]>('/rules')
}

/** 获取白名单场景列表 */
export function getAllowlistScenes(): Promise<AllowlistScene[]> {
  return request<AllowlistScene[]>('/allowlist/scenes')
}

/** 更新白名单 */
export function updateAllowlist(data: AllowlistUpdateRequest): Promise<{ status: string; scene_type: string }> {
  return request('/allowlist', {
    method: 'POST',
    body: data,
  })
}

/** 获取 Anti-AI 系统统计 */
export function getAntiAIStats(novelId?: string): Promise<AntiAIStats> {
  const query = novelId ? `?novel_id=${encodeURIComponent(novelId)}` : ''
  return request<AntiAIStats>(`/stats${query}`)
}

export function getAuditRuns(novelId: string): Promise<AntiAIAuditRun[]> {
  return request(`/runs/${encodeURIComponent(novelId)}`)
}

export function getStreamRuns(novelId: string): Promise<AntiAIStreamRun[]> {
  return request(`/stream-runs/${encodeURIComponent(novelId)}`)
}

export function getPromptRuns(novelId: string): Promise<AntiAIPromptRun[]> {
  return request(`/prompt-runs/${encodeURIComponent(novelId)}`)
}

export function getAuditProgress(novelId: string): Promise<AntiAIScanProgress[]> {
  return request(`/audit-progress/${encodeURIComponent(novelId)}`)
}

export function getAuditRun(novelId: string, runId: string): Promise<AntiAIAuditRun> {
  return request(`/runs/${encodeURIComponent(novelId)}/${encodeURIComponent(runId)}`)
}

export function getAuditHistory(novelId: string): Promise<Record<string, unknown>[]> {
  return request(`/audits/${encodeURIComponent(novelId)}`)
}

export function getAuditTrend(novelId: string, lastN = 20): Promise<AntiAITrendPoint[]> {
  return request(`/trend/${encodeURIComponent(novelId)}?last_n=${lastN}`)
}

function candidateBase(novelId: string): string {
  return `/novels/${encodeURIComponent(novelId)}`
}

export function getRewritePolicy(novelId: string): Promise<AntiAIRewritePolicy> {
  return request(`${candidateBase(novelId)}/rewrite-policy`)
}

export function saveRewritePolicy(novelId: string, policy: AntiAIRewritePolicy): Promise<AntiAIRewritePolicy> {
  return request(`${candidateBase(novelId)}/rewrite-policy`, { method: 'PUT', body: policy })
}

export function getRewriteCandidates(novelId: string): Promise<RewriteCandidate[]> {
  return request(`${candidateBase(novelId)}/candidates`)
}

export function getRewriteCandidate(novelId: string, candidateId: string): Promise<RewriteCandidate> {
  return request(`${candidateBase(novelId)}/candidates/${encodeURIComponent(candidateId)}`)
}

export function confirmRewriteCandidate(novelId: string, candidateId: string, tokens: CandidateConfirmTokens): Promise<{ status: string; candidate: RewriteCandidate; requires_resume: boolean }> {
  return request(`${candidateBase(novelId)}/candidates/${encodeURIComponent(candidateId)}/confirm`, { method: 'POST', body: tokens })
}

export function rejectRewriteCandidate(novelId: string, candidateId: string, hash: string): Promise<RewriteCandidate> {
  return request(`${candidateBase(novelId)}/candidates/${encodeURIComponent(candidateId)}/reject`, {
    method: 'POST', body: { expected_candidate_hash: hash },
  })
}

export function regenerateRewriteCandidate(novelId: string, candidate: RewriteCandidate): Promise<{ status: string; candidate?: RewriteCandidate; error?: string }> {
  return request(`${candidateBase(novelId)}/chapters/${candidate.chapter_number}/rewrite-candidates`, {
    method: 'POST', body: { expected_base_hash: candidate.current_content_hash,
      expected_base_revision: candidate.current_revision, expected_run_epoch: candidate.run_epoch },
  })
}
