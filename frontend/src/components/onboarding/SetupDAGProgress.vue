<template>
  <section class="setup-dag-progress" aria-label="新书生成进度">
    <div class="setup-dag-head">
      <div class="setup-dag-status" aria-live="polite">
        <strong>{{ currentLabel }}</strong>
        <span v-if="run">{{ statusLabel }} · 已用 {{ elapsedLabel }}</span>
        <span v-else>{{ loading ? '正在读取工作流…' : '生成时可查看每个步骤的执行进度' }}</span>
      </div>
      <n-space :size="8">
        <n-button size="small" secondary @click="openStage(currentStage || run?.stage || visibleStages[0]?.id)">查看 DAG 工作流</n-button>
        <n-button v-if="busy" size="small" secondary :loading="stopping" @click="$emit('stop')">停止生成</n-button>
        <n-button v-if="error" size="small" secondary :loading="loading" @click="$emit('reconnect')">重新连接</n-button>
      </n-space>
    </div>
    <n-progress v-if="run" type="line" :percentage="run.progress.percent" :status="run.status === 'error' ? 'error' : run.status === 'completed' ? 'success' : 'default'" :height="8" />
    <p v-if="run" class="setup-dag-message">{{ run.progress.message }} · {{ run.progress.completed }}/{{ run.progress.total }} 个节点完成</p>
    <SetupModelCallStatus v-if="run?.status === 'running'" :progress="activeModelCall" />
    <p v-if="run?.resumed_node_count" class="setup-dag-message">已沿用 {{ run.resumed_node_count }} 个成功步骤，从上次进度继续。</p>
    <n-alert v-if="error" type="error" :show-icon="false">{{ error }}</n-alert>
    <div v-if="unitProgress.length" class="setup-dag-units" aria-label="分项审核与保存进度">
      <div v-for="unit in unitProgress" :key="unit.id" class="setup-dag-unit" :class="`setup-dag-unit--${unit.state}`">
        <strong>{{ unit.label }}</strong><span>{{ unit.message }}</span>
      </div>
    </div>
    <div class="setup-dag-stages">
      <button v-for="stage in stages" :key="stage.id" type="button" class="setup-dag-stage" :class="{ 'is-active': stage.id === (diagramOpen ? selectedStage : currentStage || run?.stage) }" :aria-label="`查看${stage.label} DAG 工作流`" @click="openStage(stage.id)">
        <strong>{{ stage.label }}</strong>
        <span>{{ stage.dependencies.length ? `依赖：${stage.dependencies.map(id => stageLabel(id)).join('、')}` : '起点' }}</span>
      </button>
    </div>
    <n-modal v-if="diagramOpen" v-model:show="diagramOpen" preset="card" class="setup-dag-diagram-modal" :title="`${stageLabel(selectedStage)} · 新书设置 DAG 工作流`" style="width: 94%; max-width: 1180px; max-height: 92vh">
      <p class="setup-dag-message">{{ historyMode ? '历史运行记录' : '当前工作流定义' }} · {{ selectedRunStatus }} · {{ nodes.length }} 个节点。点击节点查看只读执行详情，拖动画布或滚轮缩放。</p>
      <div class="setup-dag-tabs" aria-label="查看阶段工作流">
        <n-button v-for="stage in visibleStages" :key="stage.id" size="small" :type="selectedStage === stage.id ? 'primary' : 'default'" @click="selectedStage = stage.id">{{ stage.label }}</n-button>
      </div>
      <div v-if="selectedRun" class="setup-dag-tabs" aria-label="图版本">
        <n-button size="small" :type="!historyMode ? 'primary' : 'default'" @click="historyMode = false">查看当前定义</n-button>
        <n-button size="small" :type="historyMode ? 'primary' : 'default'" @click="historyMode = true">查看历史记录</n-button>
        <span class="setup-dag-message">运行 {{ selectedRun.run_id }} · v{{ selectedRun.workflow_version ?? 1 }}</span>
      </div>
      <n-alert v-if="isLegacyRun" type="warning" :show-icon="false" class="setup-dag-notice">旧版记录不含新增审核。{{ historyMode ? '当前展示当时真实执行的节点、状态与输出。' : '当前展示新版完整流程；旧版成功状态不代表新增审核已执行。可切换历史记录查看当时结果。' }}</n-alert>
      <n-alert v-else-if="selectedRun && (selectedRun.workflow_version ?? 1) < 3" type="warning" :show-icon="false" class="setup-dag-notice">此历史工作流仅含本地规则扫描，未包含新版模型语义审稿。重新生成当前阶段后执行完整审稿，历史报告仍可查看。</n-alert>
      <div v-if="auditShortcuts.length" class="setup-dag-audit-shortcuts" aria-label="分项审核报告快捷入口">
        <div v-for="unit in auditShortcuts" :key="unit.id" class="setup-dag-audit-shortcut">
          <strong>{{ unit.label }}</strong>
          <n-button v-for="audit in unit.nodes" :key="audit.id" size="tiny" secondary :aria-label="`${unit.label} · ${audit.label}`" @click="selectedNodeId = audit.id">{{ audit.label }}</n-button>
        </div>
      </div>
      <div v-if="nodes.length" class="setup-dag-canvas">
        <DAGCanvas :key="`${selectedStage}-${historyMode}-${selectedRun?.run_id || 'pending'}`" :novel-id="novelId" :nodes="nodes" :edges="edges" @node-detail="selectedNodeId = $event" />
      </div>
      <n-empty v-else description="工作流尚未加载，请检查连接后重新连接" />
      <div class="setup-dag-legend"><span>灰色：等待</span><span>蓝色：运行</span><span>绿色：完成</span><span>红色：失败 / 中断</span></div>
      <SetupDAGNodeDetail v-if="selectedNode" :node="selectedNode" :run="selectedRun" :state="nodeState(selectedNode)" :stage-label="stageLabel(selectedStage)" @close="selectedNodeId = null" />
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Node, Edge } from '@vue-flow/core'
import DAGCanvas from '@/components/autopilot/DAGCanvas.vue'
import SetupDAGNodeDetail from './SetupDAGNodeDetail.vue'
import SetupModelCallStatus from './SetupModelCallStatus.vue'
import type { SetupDagRun, SetupDagStage, SetupGraphNode, SetupStageId } from '@/api/setupDag'

const props = defineProps<{ novelId: string; currentStage?: SetupStageId; stages: SetupDagStage[]; run: SetupDagRun | null; busy: boolean; loading: boolean; stopping: boolean; error: string }>()
defineEmits<{ stop: []; reconnect: [] }>()
const diagramOpen = ref(false)
const selectedStage = ref<SetupStageId>('worldbuilding')
const historyMode = ref(false)
const selectedNodeId = ref<string | null>(null)
function openStage(stage?: SetupStageId) {
  selectedStage.value = stage || 'worldbuilding'
  historyMode.value = false
  selectedNodeId.value = null
  diagramOpen.value = true
}
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null
watch(() => props.run?.status, status => {
  if (ticker) clearInterval(ticker)
  ticker = null
  now.value = Date.now()
  if (status === 'running') ticker = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onBeforeUnmount(() => { if (ticker) clearInterval(ticker) })
const currentLabel = computed(() => props.run?.progress.current_node_label || (props.busy ? '正在创建生成任务…' : '新书设置工作流'))
const activeModelCall = computed(() => {
  const run = props.run
  const node = run?.graph.nodes.find(item => item.id === run.progress.current_node_id)
  const vars = node ? variables(node) : undefined
  return vars && ['deviation', 'anti_ai', 'commit'].includes(vars.action || '')
    ? run?.checkpoint?.units?.[vars.unit || run.stage]?.model_call_progress : undefined
})
const statusLabel = computed(() => props.run ? { running: '生成中', completed: '执行完成', error: '生成失败', cancelled: '已停止', interrupted: '已中断' }[props.run.status] : '尚未执行')
const elapsedLabel = computed(() => {
  const start = Date.parse(props.run?.started_at || '')
  const end = props.run?.completed_at ? Date.parse(props.run.completed_at) : now.value
  const seconds = Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, Math.floor((end - start) / 1000)) : 0
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
})
function stageLabel(id: SetupStageId) { return props.stages.find(stage => stage.id === id)?.label || id }
const visibleStages = computed(() => props.stages.length ? props.stages : props.run ? [{ id: props.run.stage, label: props.run.stage, dependencies: [], graph: props.run.graph, latest_run: props.run }] : [])
const selectedDefinition = computed(() => visibleStages.value.find(stage => stage.id === selectedStage.value))
const selectedRun = computed(() => props.run?.stage === selectedStage.value ? props.run : selectedDefinition.value?.latest_run || null)
const selectedRunStatus = computed(() => selectedRun.value ? !historyMode.value && isLegacyRun.value ? '新版定义尚未执行' : { running: '生成中', completed: '执行完成', error: '生成失败', cancelled: '已停止', interrupted: '已中断' }[selectedRun.value.status] : '尚未执行')
const isLegacyRun = computed(() => !!selectedRun.value && (selectedRun.value.workflow_version ?? 1) < 2)
const selectedGraph = computed(() => historyMode.value && selectedRun.value ? selectedRun.value.graph : selectedDefinition.value?.graph)
const selectedNode = computed(() => selectedGraph.value?.nodes.find(node => node.id === selectedNodeId.value))
watch(selectedStage, () => { historyMode.value = false; selectedNodeId.value = null })
watch([historyMode, () => selectedRun.value?.run_id, diagramOpen], () => { selectedNodeId.value = null })
const unitLabels: Record<string, string> = { style: '文风公约', core_rules: '核心法则', geography: '地理生态', society: '社会结构', culture: '历史文化', daily_life: '沉浸感细节', world_review: '完整世界观复审' }
function variables(node: { config: Record<string, unknown> }) { return (node.config.prompt_variables || {}) as { action?: string; unit?: string } }
const auditShortcuts = computed(() => {
  const audits = (selectedGraph.value?.nodes || []).filter(node => ['deviation', 'anti_ai'].includes(variables(node).action || ''))
  const units = [...new Set(audits.map(node => variables(node).unit || selectedStage.value))]
  return units.map(id => ({ id, label: unitLabels[id] || stageLabel(selectedStage.value), nodes: audits.filter(node => (variables(node).unit || selectedStage.value) === id).map(node => ({ id: node.id, label: variables(node).action === 'deviation' ? '偏离检测' : 'Anti-AI' })) }))
})
function nodeState(node: SetupGraphNode) {
  const run = selectedRun.value
  if (!run || (!historyMode.value && isLegacyRun.value)) return undefined
  const executed = run.graph.nodes.find(item => item.id === node.id)
  if (!executed) return undefined
  const current = variables(node), actual = variables(executed)
  if (current.unit !== actual.unit || current.action !== actual.action) return undefined
  return run.node_states[node.id]
}
const unitProgress = computed(() => {
  const run = props.run
  if (!run) return []
  const units = [...new Set(run.graph.nodes.map(node => variables(node).unit).filter((unit): unit is string => Boolean(unit)))]
  return units.map(id => {
    const steps = run.graph.nodes.filter(node => variables(node).unit === id)
    const verify = steps.find(node => variables(node).action === 'verify')
    const saved = verify && ['success', 'completed'].includes(run.node_states[verify.id]?.status || '')
    const failed = steps.some(node => ['error', 'interrupted'].includes(run.node_states[node.id]?.status || ''))
    const active = steps.find(node => run.node_states[node.id]?.status === 'running')
    return { id, label: unitLabels[id] || stageLabel(run.stage), state: saved ? 'saved' : failed ? 'error' : active ? 'running' : 'pending',
      message: saved ? (run.workflow_version ?? 1) >= 3 ? '审核通过，已保存并回读' : (run.workflow_version ?? 1) >= 2 ? '历史规则审核，已保存并回读（未含语义审稿）' : '旧版已保存并回读（无新增审核记录）' : failed ? '未完成，继续后重试' : active ? active.label : '等待前项完成' }
  })
})
const nodes = computed<Node[]>(() => {
  const stage = selectedDefinition.value, run = selectedRun.value, graph = selectedGraph.value
  if (!stage || !graph) return []
  const units = [...new Set(graph.nodes.map(node => variables(node).unit || stage.id))]
  return graph.nodes.map((node, index) => {
    const actualState = nodeState(node)
    const state = actualState?.status || 'pending'
    const active = !!actualState && run?.status === 'running' && run.progress.current_node_id === node.id
    const { unit, action } = variables(node)
    const unitIndex = units.indexOf(unit || stage.id)
    const column = unit ? graph.nodes.slice(0, index).filter(item => variables(item).unit === unit).length : index
    const audit = ['validate', 'deviation', 'anti_ai'].includes(action || '')
    return { id: node.id, type: 'dagCustom', position: { x: column * 255, y: unitIndex * 170 },
      data: { ...node, enabled: true, isSelected: false,
        meta: { display_name: unitLabels[unit || ''] || stage.label, icon: audit ? '🔍' : action === 'commit' || action === 'verify' ? '✓' : '✦', category: audit ? 'validation' : 'execution',
          input_ports: [{ name: 'input', data_type: 'json', required: false }], output_ports: [{ name: 'output', data_type: 'json', required: false }] },
        runState: { status: state === 'interrupted' ? 'error' : state, metrics: {}, duration_ms: actualState?.duration_ms || 0, progress: active ? 0.5 : 0 } },
      class: active ? 'is-current' : '' }
  })
})
const edges = computed<Edge[]>(() => (selectedGraph.value?.edges || []).map(edge => ({ ...edge, type: 'custom', sourceHandle: 'output', targetHandle: 'input',
  data: { condition: 'always', isActive: !isLegacyRun.value && selectedRun.value?.status === 'running' && selectedRun.value.progress.current_node_id === edge.target } })))
</script>

<style scoped>
.setup-dag-progress { display: grid; gap: 10px; padding: 14px; margin-top: 16px; background: var(--app-surface-raised, var(--app-surface)); border: 1px solid var(--app-border); border-radius: 10px; }
.setup-dag-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.setup-dag-status { display: grid; gap: 4px; color: var(--app-text-primary); }
.setup-dag-status span,.setup-dag-message { margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.6; }
.setup-dag-stages { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.setup-dag-stage { display: grid; gap: 4px; padding: 8px; border: 1px solid transparent; border-radius: 6px; background: var(--app-surface); color: var(--app-text-secondary); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.setup-dag-stage:hover,.setup-dag-stage:focus-visible { border-color: var(--color-brand, #2080f0); outline: none; }
.setup-dag-stage span { color: var(--app-text-muted); font-size: 11px; }
.setup-dag-stage.is-active { box-shadow: inset 0 0 0 1px var(--color-brand, #2080f0); }
.setup-dag-canvas { height: min(62vh, 560px); min-height: 240px; margin-top: 12px; border: 1px solid var(--app-border); border-radius: 8px; overflow: hidden; }
.setup-dag-legend { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; color: var(--app-text-muted); font-size: 12px; }
.setup-dag-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.setup-dag-notice { margin-top: 12px; }
.setup-dag-audit-shortcuts { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; margin-top: 12px; }
.setup-dag-audit-shortcut { display: flex; align-items: center; gap: 6px; padding: 8px; border: 1px solid var(--app-border); border-radius: 6px; }
.setup-dag-audit-shortcut strong { margin-right: auto; font-size: 12px; color: var(--app-text-secondary); }
:global(.setup-dag-diagram-modal > .n-card-content) { min-height: 0; overflow-y: auto; }
.setup-dag-units { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 8px; }
.setup-dag-unit { display: grid; gap: 4px; padding-left: 8px; border-left: 3px solid var(--app-border); font-size: 12px; }
.setup-dag-unit span { color: var(--app-text-muted); line-height: 1.5; overflow-wrap: anywhere; }
.setup-dag-unit--saved { border-color: var(--color-success); }
.setup-dag-unit--running { border-color: var(--color-brand); }
.setup-dag-unit--error { border-color: var(--color-danger); }
@media (max-width: 700px) { .setup-dag-stages { grid-template-columns: repeat(2, minmax(0, 1fr)); } .setup-dag-canvas { height: 400px; } }
</style>
