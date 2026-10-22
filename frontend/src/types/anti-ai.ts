/**
 * Anti-AI 防御系统 — 前端类型定义
 *
 * 七层纵深防御体系类型：
 * L1 正向行为映射 → L2 协议规则 → L3 白名单 → L4 角色状态锁
 * L5 上下文配额 → L6 Token拦截 → L7 章后审计
 */

// ─── 扫描结果 ───

export interface ClicheHit {
  pattern: string
  text: string
  start: number
  end: number
  severity: 'critical' | 'warning' | 'info'
  category: string
  replacement_hint: string
}

export interface ScanResult {
  run_id?: string
  scan_status?: 'completed' | 'failed' | 'unavailable'
  persistence_status?: 'not_requested' | 'committed' | 'failed'
  source?: 'manual'
  persisted?: boolean
  rule_version?: string
  content_hash?: string
  total_hits: number
  critical_hits: number
  warning_hits: number
  severity_score: number
  overall_assessment: '纯净' | '轻微' | '中等' | '严重' | '未检测'
  category_distribution: Record<string, number>
  top_patterns: string[]
  recommendations: string[]
  improvement_suggestions: string[]
  hits: ClicheHit[]
}

// ─── 分类信息 ───

export interface PromptCategory {
  key: string
  name: string
  icon: string
  description: string
  color: string
  sort_order: number
  prompt_count: number
}

// ─── 规则信息 ───

export interface AntiAIRule {
  key: string
  anti_pattern: string
  positive_action: string
  category: string
  severity: 'critical' | 'warning' | 'info'
}

// ─── 白名单 ───

export interface AllowlistScene {
  scene_type: string
  allowed_categories: string[]
  allowed_patterns: string[]
  max_density_per_1000: number
  description: string
}

export interface AllowlistUpdateRequest {
  scene_type: string
  allowed_categories?: string[]
  allowed_patterns?: string[]
  max_density_per_1000?: number
  description?: string
}

// ─── 系统统计 ───

export interface AntiAIStats {
  total_prompts: number
  anti_ai_prompts: number
  categories_count: number
  cliche_patterns: number
  layers: {
    L1_positive_framing: number
    L2_protocol_rules: number
    L3_allowlist_scenes: number
    L4_state_vector: string
    L5_context_quota: string
    L6_token_guard: string
    L7_audit: string
  }
  layer_states?: Record<string, {
    implemented: boolean
    configured: boolean
    connected: boolean
    observed: boolean
    failed: boolean
    status: 'implemented' | 'configured' | 'connected' | 'observed' | 'failed' | 'unavailable'
    error?: string
    connection_evidence?: string
    latest_execution?: { status: string; applied: boolean; reason?: string | null }
  }>
  observed_audit_runs?: number
  failed_audit_runs?: number
  observed_stream_runs?: number
  failed_stream_runs?: number
  novel_id?: string | null
  latest_audit_run?: { run_id: string; chapter_number: number; scan_status: string; persistence_status: string } | null
}

export interface AntiAIPromptRun {
  run_id: string
  job_id: string
  chapter_number: number | null
  status: string
  prompt_hash: string
  rule_version: string
  error?: string | null
  evidence: { scene_type: string; input_token_estimate: number; input_budget_tokens: number;
    layers: Record<string, { status: string; applied: boolean; reason?: string | null }> }
}

export interface AntiAIStreamRun {
  run_id: string
  job_id: string
  chapter_number: number | null
  status: 'completed' | 'failed' | 'interrupted' | 'cancelled'
  scan_status: string
  persistence_status: string
  content_hash: string
  rule_version: string
  action: 'warn' | 'cancel'
  error?: string | null
  summary: { chunks_scanned: number; incremental_hits: number; final_scan_status: string; final_total_hits?: number }
}

export interface AntiAIAuditRun {
  run_id: string
  chapter_number: number
  content_hash: string
  rule_version: string
  scan_status: string
  persistence_status: string
  error?: string | null
  created_at: string
  report: {
    metrics: { severity_score: number; overall_assessment: string; total_hits: number }
    recommendations: string[]
    hits: ClicheHit[]
  }
}

export interface AntiAIScanProgress {
  run_id: string
  chapter_number: number
  stage: 'scanning' | 'persisting' | 'completed' | 'failed'
  scan_status: string
  persistence_status: string
  content_hash: string
  rule_version: string
  job_id: string
  chapter_revision: number | null
  error?: string | null
}

export interface AntiAITrendPoint {
  chapter_number: number
  severity_score: number
  hit_density: number
  overall_assessment: string
  critical_hits: number
  total_hits: number
  run_id?: string | null
  content_hash?: string | null
  rule_version?: string | null
  latest_scan_status?: string | null
  latest_persistence_status?: string | null
  legacy?: boolean
}

export interface AntiAIRewritePolicy {
  mode: 'scan_only' | 'review' | 'auto'
  max_attempts: number
  max_severity: number
  stream_action?: 'warn' | 'cancel'
  stream_critical_limit?: number
  scene_type?: string
}

export interface RewriteCandidate {
  candidate_id: string
  run_id: string
  novel_id: string
  chapter_number: number
  kind: string
  attempt: number
  base_hash: string
  base_revision: number
  base_content: string
  content: string
  content_hash: string
  diff: string
  status: string
  error?: string
  created_at: string
  committed_at?: string
  stale: boolean
  is_generating: boolean
  can_confirm: boolean
  run_epoch: number
  novel_stage: string
  current_revision: number | null
  current_content_hash: string | null
  audit: {
    accepted?: boolean
    reasons?: string[]
    scan?: { scan_status: string; metrics: { severity_score: number; critical_hits: number } }
    quality?: { status: string; passed: boolean; error?: string }
    voice?: { status: string; similarity_score?: number | null; baseline_available?: boolean; error?: string }
    committed_revision?: number
    final_audit?: { scan_status: string; persistence_status: string; content_hash: string; run_id: string }
    aftermath?: { narrative_sync_ok: boolean; derivation_status?: string; error?: string }
  }
}

export interface CandidateConfirmTokens {
  expected_candidate_hash: string
  expected_base_hash: string
  expected_base_revision: number
  expected_run_epoch: number
}

// ─── 严重性颜色映射 ───

export const SEVERITY_COLORS: Record<string, string> = {
  critical: '#dc2626',
  warning: '#f59e0b',
  info: '#3b82f6',
}

export const ASSESSMENT_COLORS: Record<string, string> = {
  '纯净': '#22c55e',
  '轻微': '#84cc16',
  '中等': '#f59e0b',
  '严重': '#dc2626',
  '未检测': '#6b7280',
}

// ─── 场景类型中文映射 ───

export const SCENE_TYPE_LABELS: Record<string, string> = {
  default: '默认',
  battle: '战斗',
  suspense: '悬疑',
  horror: '恐怖',
  confession: '告白',
  revelation: '揭秘/反转',
}
