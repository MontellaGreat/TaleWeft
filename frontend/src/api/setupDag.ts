import { apiClient } from './config'

export type SetupStageId = 'worldbuilding' | 'characters' | 'locations' | 'plot_outline'
export type SetupRunStatus = 'running' | 'completed' | 'error' | 'cancelled' | 'interrupted'
export type SetupNodeStatus = 'pending' | 'running' | 'success' | 'completed' | 'error' | 'bypassed' | 'interrupted'
export interface SetupGraphNode {
  id: string
  type: string
  label: string
  position: { x: number; y: number }
  config: Record<string, unknown>
}
export interface SetupGraph {
  nodes: SetupGraphNode[]
  edges: Array<{ id: string; source: string; target: string }>
}
export interface SetupDagNodeState {
  status: SetupNodeStatus
  outputs?: Record<string, unknown>
  execution_receipt?: Record<string, unknown>
  duration_ms?: number
  error?: string | null
  reused_from_run_id?: string
}
export interface SetupDagUnitCheckpoint {
  model_call_progress?: SetupModelCallProgress
  payload?: unknown
  audits?: { deviation?: Record<string, unknown>; anti_ai?: Record<string, unknown> }
  audit_history?: unknown[]
  total_repairs?: Record<string, number>
  repair_counts?: Record<string, number>
  model_calls?: unknown[]
  attempt_id?: string
  session_id?: string
  validated_hash?: string
  committed_hash?: string
  superseded_by?: string
  revision_commit_id?: string
  approved_value_hash?: string
  [key: string]: unknown
}
export interface SetupModelCallProgress {
  purpose: string
  status: 'streaming' | 'retry_wait' | 'completed' | 'failed' | 'cancelled' | 'timed_out'
  call_number: number
  max_calls: number
  retry_at?: number
  retry_after_seconds?: number
  started_at?: number
  received_characters?: number
  last_content_at?: number
  timeout_seconds?: number
  reused?: boolean
}
export interface SetupDagRun {
  run_id: string
  novel_id: string
  stage: SetupStageId
  status: SetupRunStatus
  workflow_version?: number
  graph: SetupGraph
  node_states: Record<string, SetupDagNodeState>
  checkpoint?: { version?: number; stage?: SetupStageId; run_id?: string; unit?: string; units?: Record<string, SetupDagUnitCheckpoint>; approved?: Record<string, unknown>; [key: string]: unknown }
  resume_from_run_id?: string | null
  resumed_node_count?: number
  progress: { completed: number; total: number; percent: number; current_node_id?: string | null; current_node_label?: string | null; message: string }
  error: string | null
  started_at: string
  completed_at: string | null
}
export interface SetupDagStage {
  id: SetupStageId
  label: string
  dependencies: SetupStageId[]
  graph: SetupGraph
  latest_run: SetupDagRun | null
}
export interface SetupDagDefinition { stages: SetupDagStage[]; active_run: SetupDagRun | null }

const root = (novelId: string) => `/dag/${encodeURIComponent(novelId)}/setup`
export const setupDagApi = {
  get: (novelId: string) => apiClient.get<SetupDagDefinition>(root(novelId)) as Promise<SetupDagDefinition>,
  start: (novelId: string, stage: SetupStageId, restart = false) => apiClient.post<SetupDagRun>(`${root(novelId)}/run`, { stage, ...(restart ? { restart: true } : {}) }) as Promise<SetupDagRun>,
  getRun: (novelId: string, runId: string) => apiClient.get<SetupDagRun>(`${root(novelId)}/runs/${encodeURIComponent(runId)}`) as Promise<SetupDagRun>,
  stop: (novelId: string, runId: string) => apiClient.post<SetupDagRun>(`${root(novelId)}/stop`, { run_id: runId }) as Promise<SetupDagRun>,
}
