import type { SetupDagRun, SetupDagStage, SetupGraph, SetupStageId } from '../../src/api/setupDag'

export const inspectionStages: SetupStageId[] = ['worldbuilding', 'characters', 'locations', 'plot_outline']
export const inspectionLabels: Record<SetupStageId, string> = { worldbuilding: '文风 / 世界观', characters: '人物', locations: '地图', plot_outline: '剧情总纲' }
const unitLabels: Record<string, string> = { style: '文风公约', core_rules: '核心法则', geography: '地理生态', society: '社会结构', culture: '历史文化', daily_life: '沉浸感细节', world_review: '完整世界观复审' }
const actions = ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
const actionLabels: Record<string, string> = { prepare: '准备上下文', generate: '生成', validate: '结构校验', deviation: '偏离检测与修复', anti_ai: 'Anti-AI 审计与修复', commit: '保存资料与关联', verify: '回读确认' }
export function inspectionGraph(stage: SetupStageId, version = 3): SetupGraph {
  const units = stage === 'worldbuilding' && version >= 2 ? Object.keys(unitLabels) : [stage]
  const nodes = units.flatMap((unit, row) => (version < 2 ? actions.filter(action => !['deviation', 'anti_ai'].includes(action)) : unit === 'world_review' ? actions.filter(action => action !== 'generate') : actions)
    .map((action, column) => ({ id: stage === 'worldbuilding' && version >= 2 ? `${stage}_${unit}_${action}` : `${stage}_${action}`, type: 'setup_step', label: `${unitLabels[unit] || inspectionLabels[stage]} · ${actionLabels[action]}`, position: { x: column * 280, y: row * 180 }, config: { prompt_variables: { action, unit } } })))
  return { nodes, edges: nodes.slice(1).map((node, i) => ({ id: `${stage}-edge-${i}`, source: nodes[i]!.id, target: node.id })) }
}
export function inspectionRun(stage: SetupStageId, version = 3): SetupDagRun {
  const graph = inspectionGraph(stage, version)
  const units: Record<string, unknown> = {}
  const node_states = Object.fromEntries(graph.nodes.map(node => {
    const { action, unit } = node.config.prompt_variables as { action: string; unit: string }
    const key = `${stage}-${unit}`
    const deviation = { passed: true, input_hash: `${key}-node-hash`, attempt_id: `${key}-attempt`, checks: { synopsis: true, genre: true, requirements: true, predecessors: true }, issues: [], evidence: `${key}-deviation-node-report` }
    const anti_ai = { passed: true, input_hash: `${key}-node-hash`, scan_status: 'completed', scan_id: `${key}-anti-ai-node-report`, reports: [{ path: 'description', scan_status: 'completed', effective_rule_count: 51, rule_version: 'fixture-rules-2', input_hash: `${key}-leaf-hash`, content_hash: `${key}-leaf-hash`, total_hits: 0, hits: [] }] }
    if (version >= 3) Object.assign(anti_ai, { semantic_review: { status: 'completed', review_version: 'semantic-style-v1', passed: true, attempt_id: `${key}-semantic-attempt`,
      checks: { vague_lyricism: true, expository_voice: true, mechanical_structure: true, emotion_labeling: true, template_closure: true },
      input_hash: `${key}-semantic-hash`, coverage: [{ path: 'description', input_hash: `${key}-leaf-hash` }], issues: [] } })
    units[unit] = { payload: { content: `${key}-current-candidate` }, audits: { deviation: { ...deviation, input_hash: `${key}-current-hash`, evidence: `${key}-deviation-current-report` }, anti_ai: { ...anti_ai, input_hash: `${key}-current-hash`, scan_id: `${key}-anti-ai-current-report` } },
      audit_history: [{ kind: 'deviation', passed: false, issues: [`${key}-repair-reason`] }, { kind: 'anti_ai', passed: true, scan_id: `${key}-history-scan` }], total_repairs: { deviation: 1, anti_ai: 2 }, attempt_id: `${key}-current-attempt`, validated_hash: `${key}-current-hash` }
    return [node.id, { status: 'success' as const, outputs: version < 2 ? { legacy_output: `${stage}-legacy-output` } : action === 'deviation' ? { deviation_audit: deviation, setup_unit: unit } : action === 'anti_ai' ? { anti_ai_audit: anti_ai, setup_unit: unit } : { setup_unit: unit, result: `${node.id}-actual-output` },
      execution_receipt: { executor_completed: true, node_id: node.id, output_hash: `${node.id}-receipt` }, duration_ms: 125 }]
  }))
  const result: SetupDagRun = { run_id: `${stage}-v${version}-run`, novel_id: 'book', stage, workflow_version: version, status: 'completed', graph, node_states,
    progress: { completed: graph.nodes.length, total: graph.nodes.length, percent: 100, current_node_id: null, current_node_label: null, message: `${inspectionLabels[stage]}已完成` }, error: null, started_at: `2026-10-24T0${inspectionStages.indexOf(stage)}:00:00Z`, completed_at: '2026-10-24T05:00:00Z' }
  return Object.assign(result, version >= 2 ? { checkpoint: { version: 2, stage, run_id: result.run_id, units } } : {})
}
export function inspectionCatalog(legacy = false): SetupDagStage[] {
  return inspectionStages.map((id, i) => ({ id, label: inspectionLabels[id], dependencies: inspectionStages.slice(0, i), graph: inspectionGraph(id), latest_run: inspectionRun(id, legacy && id !== 'locations' ? 1 : 3) }))
}
