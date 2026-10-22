import { apiRoutes } from './endpoints'
import { fetchJson, fetchOk, fetchUrl, HttpError, type FetchJsonOptions } from './http'
import type { DataChangeProposal, DataChangeReceipt } from './dataProtection'

export interface ContinuationStructureNode {
  title: string
  description?: string
  volumes?: ContinuationStructureNode[]
  acts?: ContinuationStructureNode[]
}

export type PipelineResultStatus = 'passed' | 'failed' | 'unavailable' | 'skipped' | 'deferred'

export interface PipelineStepResult {
  status: PipelineResultStatus
  passed: boolean
  skip: boolean
  message: string
  score: number | null
  violations: Record<string, unknown>[]
  suggestions: string[]
  metadata: Record<string, unknown>
}

export type AutopilotStatus = Record<string, any> & {
  active_pipeline_step?: string
  active_pipeline_run_id?: string
  last_stable_stage?: string
  autopilot_run_epoch?: number
  autopilot_recovery_reason?: string
  pipeline_status?: PipelineResultStatus
  pipeline_passed?: boolean
  pipeline_success?: boolean
  pipeline_complete?: boolean
  pipeline_step_status?: Record<string, PipelineResultStatus>
  pipeline_step_results?: Record<string, PipelineStepResult>
  pipeline_error?: string
}

export interface AutopilotStartRequest {
  max_auto_chapters: number
  target_chapters: number
  target_words_per_chapter: number
}

export interface AutopilotResumeResponse {
  current_stage?: string
  message?: string
  [key: string]: unknown
}

export interface ContinuationProposal {
  proposal_id: string
  novel_id: string
  mode: 'continue' | 'replan' | string
  base_stage: string
  base_target_chapters: number
  proposed_target_chapters: number
  completed_chapters: number
  from_chapter: number
  rationale: string
  status: 'proposed' | 'approved' | 'macro_planning' | 'awaiting_structure_review' | 'applied' | 'rejected' | 'legacy_unverified' | string
  base_run_epoch?: number
  run_epoch?: number | null
  structure_change_id?: string | null
  structure?: ContinuationStructureNode[] | null
  structure_review?: DataChangeProposal
  structure_confirmed_at?: string | null
  command_id?: string | null
  created_at: string
  approved_at?: string | null
  plan: {
    next_stage?: string
    additional_chapters?: number
    requires_structure_review?: boolean
    preserves_completed_chapters?: boolean
    steps?: string[]
    [key: string]: unknown
  }
}

export interface AutopilotErrorRecord {
  message: string
  timestamp: string
  context?: string
}

export interface AutopilotCircuitBreakerData {
  status: 'closed' | 'open' | 'half_open'
  error_count: number
  max_errors: number
  last_error?: AutopilotErrorRecord
  error_history?: AutopilotErrorRecord[]
}

export const autopilotApi = {
  getStatus(novelId: string, options?: FetchJsonOptions): Promise<AutopilotStatus> {
    return fetchJson<AutopilotStatus>(apiRoutes.autopilot.status(novelId), options)
  },

  getCommand(novelId: string, commandId: string, options?: FetchJsonOptions): Promise<Record<string, any>> {
    return fetchJson(`${apiRoutes.autopilot.root(novelId)}/commands/${encodeURIComponent(commandId)}`, options)
  },

  start(novelId: string, data: AutopilotStartRequest): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.start(novelId), {
      method: 'POST',
      body: data,
    })
  },

  stop(novelId: string, timeoutMs?: number): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.stop(novelId), {
      method: 'POST',
      timeoutMs,
    })
  },

  resume(novelId: string): Promise<AutopilotResumeResponse> {
    return fetchJson<AutopilotResumeResponse>(apiRoutes.autopilot.resume(novelId), {
      method: 'POST',
    })
  },

  listContinuationProposals(novelId: string): Promise<ContinuationProposal[]> {
    return fetchJson<ContinuationProposal[]>(apiRoutes.autopilot.continuationProposals(novelId))
  },

  getContinuationProposal(novelId: string, proposalId: string): Promise<ContinuationProposal> {
    return fetchJson<ContinuationProposal>(apiRoutes.autopilot.continuationProposal(novelId, proposalId))
  },

  confirmContinuationStructure(novelId: string, proposal: ContinuationProposal): Promise<DataChangeReceipt> {
    const review = proposal.structure_review
    if (!review) return Promise.reject(new Error('续写结构审阅凭据缺失，请刷新'))
    return fetchJson<DataChangeReceipt>(apiRoutes.autopilot.continuationProposal(novelId, proposal.proposal_id) + '/structure/confirm', {
      method: 'POST',
      body: { expected_hash: review.base_hash, proposal_hash: review.proposal_hash, run_epoch: review.run_epoch },
    })
  },

  createContinuationProposal(
    novelId: string,
    data: { target_chapters: number; mode?: 'continue' | 'replan'; rationale?: string },
  ): Promise<ContinuationProposal> {
    return fetchJson<ContinuationProposal>(apiRoutes.autopilot.continuationProposals(novelId), {
      method: 'POST',
      body: data,
    })
  },

  approveContinuationProposal(
    novelId: string,
    proposalId: string,
    targetWordsPerChapter?: number,
  ): Promise<Record<string, unknown>> {
    return fetchJson<Record<string, unknown>>(
      apiRoutes.autopilot.continuationProposal(novelId, proposalId) + '/approve',
      {
        method: 'POST',
        body: targetWordsPerChapter == null ? {} : { target_words_per_chapter: targetWordsPerChapter },
      },
    )
  },

  rejectContinuationProposal(novelId: string, proposalId: string): Promise<ContinuationProposal> {
    return fetchJson<ContinuationProposal>(
      apiRoutes.autopilot.continuationProposal(novelId, proposalId) + '/reject',
      { method: 'POST' },
    )
  },

  getCircuitBreaker(novelId: string): Promise<AutopilotCircuitBreakerData> {
    return fetchJson<AutopilotCircuitBreakerData>(apiRoutes.autopilot.circuitBreaker(novelId))
  },

  resetCircuitBreaker(novelId: string): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.circuitBreakerReset(novelId), {
      method: 'POST',
    })
  },

  streamUrl(novelId: string, afterSeq?: number): string {
    const params = afterSeq && afterSeq > 0 ? { after_seq: afterSeq } : undefined
    return fetchUrl(apiRoutes.autopilot.stream(novelId, params))
  },

  logStreamUrl(novelId: string): string {
    return fetchUrl(apiRoutes.autopilot.logStream(novelId))
  },
}

export function isAutopilotNotFoundError(error: unknown): boolean {
  return error instanceof HttpError && error.status === 404
}

export function isAutopilotHttpError(error: unknown): boolean {
  return error instanceof HttpError
}

export function getAutopilotHttpStatus(error: unknown): number | null {
  return error instanceof HttpError ? error.status : null
}

export function getAutopilotErrorDetail(error: unknown): string {
  if (!(error instanceof HttpError)) return ''
  const body = error.body
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    return typeof detail === 'string' ? detail : ''
  }
  return ''
}
