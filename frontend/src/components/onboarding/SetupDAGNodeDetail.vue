<template>
  <n-modal :show="true" preset="card" :title="`${stageLabel} · ${node.label}`" class="setup-node-detail" style="width: 94%; max-width: 900px; max-height: 90vh" @update:show="handleShow">
    <div :key="`${run?.run_id}-${node.id}`" class="setup-node-detail__body">
      <p class="setup-node-detail__summary">只读执行详情 · {{ statusLabel }}<span v-if="state?.duration_ms !== undefined"> · 耗时 {{ state.duration_ms }} ms</span></p>
      <SetupDetailDisclosure title="节点与运行信息"><p class="setup-node-detail__meta">节点：{{ node.id }}<br>运行：{{ run?.run_id || '尚无运行记录' }}<span v-if="run"> · 工作流 v{{ run.workflow_version ?? 1 }}</span></p></SetupDetailDisclosure>
      <n-alert v-if="legacy" type="warning" :show-icon="false">旧版记录不含新增审核，不能据此判断偏离检测或 Anti-AI 已通过。</n-alert>
      <n-alert v-else-if="run && (run.workflow_version ?? 1) < 3" type="warning" :show-icon="false">此历史工作流仅含本地规则扫描，未包含新版模型语义审稿。重新生成当前阶段后才会执行完整审稿；已保存资料和历史报告仍可查看。</n-alert>
      <n-alert v-if="!state && run" type="info" :show-icon="false">本节点尚未在此运行中执行；当前图展示新版定义。</n-alert>
      <n-alert v-if="state?.error" type="error" :show-icon="false">{{ state.error }}</n-alert>
      <SetupModelCallStatus v-if="state?.status === 'running'" :progress="unitCheckpoint?.model_call_progress" />
      <n-alert v-if="superseded" type="warning" :show-icon="false">此候选已被完整复审替代。原单元候选与审核报告仅作历史记录；请查看完整世界观复审（{{ unitCheckpoint?.superseded_by }}）节点的报告和最终保存结果。</n-alert>
      <p v-if="state?.reused_from_run_id" class="setup-node-detail__meta">沿用成功节点：{{ state.reused_from_run_id }}</p>

      <section v-if="isAudit" class="setup-node-detail__section" aria-label="本节点的审核报告">
        <h3>本节点的审核报告</h3>
        <p>来自本节点的实际输出，保留执行当时的报告。</p>
        <template v-if="nodeAudit">
          <SetupDAGAuditReport :report="nodeAudit" :kind="auditKind" />
          <p v-if="versionMismatch" class="setup-node-detail__version-notice">报告与当前候选的版本不一致；本报告不代表当前候选的审核结果。</p>
          <SetupDetailDisclosure title="报告对应的内容版本（技术详情）"><p class="setup-node-detail__hash">{{ nodeHashComparison }}</p><p>哈希一致仅确认报告与文本对应，不代表文风审核通过。</p></SetupDetailDisclosure>
        </template>
        <p v-else>暂无本节点的审核报告{{ legacy ? '：旧版工作流未包含此项审核。' : '。' }}</p>
      </section>

      <section class="setup-node-detail__section" aria-label="执行输出与回执">
        <SetupDetailDisclosure title="执行输出与回执">
        <SetupDetailDisclosure title="节点实际输出"><pre v-if="state?.outputs">{{ format(state.outputs) }}</pre><p v-else>暂无节点输出。</p></SetupDetailDisclosure>
        <SetupDetailDisclosure title="执行回执"><pre v-if="state?.execution_receipt">{{ format(state.execution_receipt) }}</pre><p v-else>暂无执行回执。</p></SetupDetailDisclosure>
        </SetupDetailDisclosure>
      </section>

      <section class="setup-node-detail__section" :aria-label="superseded ? '被完整复审替代的候选' : '该单元当前候选'">
        <SetupDetailDisclosure :title="superseded ? '被完整复审替代的候选（历史）' : '该单元当前候选'" :initially-open="checkpointAutoOpen">
        <p>下方来自当前检查点，不代表本节点执行时的候选。节点执行后可能经过修复或复审。</p>
        <template v-if="unitCheckpoint">
          <p class="setup-node-detail__meta">单元：{{ unitLabel }}</p>
          <SetupDetailDisclosure :title="superseded ? '查看被替代的候选与已确认内容' : '查看当前候选内容'">
          <div v-if="superseded" class="setup-node-detail__replacement">
            <strong>完整复审后的已确认内容（检查点 approved）</strong>
            <p class="setup-node-detail__meta">替代提交：{{ unitCheckpoint.revision_commit_id || '暂无' }}<br>已确认值 hash：{{ unitCheckpoint.approved_value_hash || '暂无' }}</p>
            <pre v-if="approvedValue !== undefined">{{ format(approvedValue) }}</pre>
            <p v-else>此快照未提供替代后的已确认内容，请查看完整世界观复审节点。</p>
            <strong>替代前的候选</strong>
          </div>
          <pre v-if="unitCheckpoint.payload !== undefined">{{ format(unitCheckpoint.payload) }}</pre>
          <p v-else>暂无当前候选。</p>
          </SetupDetailDisclosure>
          <SetupDetailDisclosure title="候选校验详情"><p>{{ superseded ? '原候选尝试' : '当前尝试' }}：{{ unitCheckpoint.attempt_id || '暂无尝试编号' }}</p><p>结构校验 hash（服务端记录）：{{ unitCheckpoint.validated_hash || '暂无' }}<br>保存 hash：{{ unitCheckpoint.committed_hash || '暂无' }}</p><p>hash 比较使用服务端检查点的结构校验记录，浏览器未重新计算候选 hash。</p></SetupDetailDisclosure>
          <SetupDetailDisclosure :title="superseded ? '被替代候选的检查点审核（历史）' : '检查点当前审核（可能晚于本节点）'" :initially-open="checkpointAutoOpen">
          <div v-for="kind in auditKinds" :key="kind.id" class="setup-node-detail__checkpoint-audit">
            <p v-if="sameAsNode(kind.id)">{{ kind.label }}：与本节点报告相同，已在上方展示。</p>
            <SetupDetailDisclosure v-else-if="unitCheckpoint.audits?.[kind.id]" :title="`${kind.label} · ${checkpointLabel(kind.id)}`" :initially-open="checkpointAutoOpen && kind.id === auditKind">
              <p class="setup-node-detail__hash">{{ compareHash(unitCheckpoint.audits[kind.id]!) }}</p>
              <SetupDAGAuditReport :report="unitCheckpoint.audits[kind.id]!" :kind="kind.id" />
            </SetupDetailDisclosure>
            <p v-else>暂无检查点审核报告。</p>
          </div>
          </SetupDetailDisclosure>
          <SetupDetailDisclosure :title="`审核历史与修复次数（${unitCheckpoint.audit_history?.length || 0} 条）`">
            <p>累计修复次数：{{ format(unitCheckpoint.total_repairs || {}) }}</p>
            <p>当前候选修复次数：{{ format(unitCheckpoint.repair_counts || {}) }}</p>
            <SetupReportList :items="unitCheckpoint.audit_history || []" label="审核历史" :page-size="5"><template #default="{ item, index }"><SetupDetailDisclosure :title="`第 ${index + 1} 条审核记录`"><pre>{{ format(item) }}</pre></SetupDetailDisclosure></template></SetupReportList>
          </SetupDetailDisclosure>
          <SetupDetailDisclosure :title="`模型调用与尝试记录（${unitCheckpoint.model_calls?.length || 0} 条）`">
            <p>会话：{{ unitCheckpoint.session_id || '暂无' }} · 当前尝试：{{ unitCheckpoint.attempt_id || '暂无' }}</p>
            <SetupReportList :items="unitCheckpoint.model_calls || []" label="模型调用" :page-size="5"><template #default="{ item }"><pre>{{ format(item) }}</pre></template></SetupReportList>
          </SetupDetailDisclosure>
        </template>
        <p v-else>{{ legacy ? '旧版记录没有当前候选与新增审核检查点。' : '暂无此单元的候选检查点。' }}</p>
        </SetupDetailDisclosure>
      </section>
    </div>
  </n-modal>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { SetupDagRun, SetupDagNodeState, SetupGraphNode } from '@/api/setupDag'
import SetupDAGAuditReport from './SetupDAGAuditReport.vue'
import SetupDetailDisclosure from './SetupDetailDisclosure.vue'
import SetupReportList from './SetupReportList.vue'
import SetupModelCallStatus from './SetupModelCallStatus.vue'

const props = defineProps<{ node: SetupGraphNode; run: SetupDagRun | null; state?: SetupDagNodeState; stageLabel: string }>()
const emit = defineEmits<{ close: [] }>()
function handleShow(show: boolean) { if (!show) emit('close') }
const vars = computed(() => (props.node.config.prompt_variables || {}) as { unit?: string; action?: string })
const unit = computed(() => vars.value.unit || props.run?.stage || '')
const unitLabel = computed(() => ({ style: '文风公约', core_rules: '核心法则', geography: '地理生态', society: '社会结构', culture: '历史文化', daily_life: '沉浸感细节', world_review: '完整世界观复审', characters: '人物', locations: '地图', plot_outline: '剧情总纲' } as Record<string, string>)[unit.value] || unit.value)
const unitCheckpoint = computed(() => props.run?.checkpoint?.units?.[unit.value])
const superseded = computed(() => Boolean(unitCheckpoint.value?.superseded_by))
const approvedValue = computed(() => props.run?.checkpoint?.approved?.[unit.value])
const legacy = computed(() => !!props.run && (props.run.workflow_version ?? 1) < 2)
const isAudit = computed(() => ['deviation', 'anti_ai'].includes(vars.value.action || ''))
const auditKind = computed(() => vars.value.action === 'deviation' ? 'deviation' : 'anti_ai')
const auditKinds = [{ id: 'deviation', label: '偏离检测' }, { id: 'anti_ai', label: 'Anti-AI' }] as const
const nodeAudit = computed(() => {
  const report = props.state?.outputs?.[vars.value.action === 'deviation' ? 'deviation_audit' : 'anti_ai_audit']
  return report && typeof report === 'object' && !Array.isArray(report) ? report as Record<string, unknown> : null
})
const statusLabel = computed(() => ({ pending: '尚未执行', running: '运行中', success: '执行成功', completed: '执行完成', error: '执行失败', interrupted: '已中断', bypassed: '已跳过' }[props.state?.status || 'pending']))
function compareHash(report: Record<string, unknown>) {
  const current = unitCheckpoint.value?.validated_hash
  const input = report.input_hash
  if (typeof input !== 'string' || !current) return '缺少报告 input_hash 或该候选的结构校验 hash，无法核对候选版本。'
  if (superseded.value) return input === current
    ? '报告 input_hash 与被替代候选的结构校验 hash 一致；仅对应历史候选，不代表完整复审后的内容。'
    : '报告 input_hash 与被替代候选的结构校验 hash 不一致；无法据此核对历史候选版本。'
  return input === current
    ? '报告 input_hash 与当前候选的服务端结构校验 hash 一致。'
    : '报告 input_hash 与当前候选的服务端结构校验 hash 不一致；本报告不代表当前候选的审核结果。'
}
const nodeHashComparison = computed(() => nodeAudit.value ? compareHash(nodeAudit.value) : '')
const versionMismatch = computed(() => nodeAudit.value?.input_hash && unitCheckpoint.value?.validated_hash && nodeAudit.value.input_hash !== unitCheckpoint.value.validated_hash)
const checkpointAutoOpen = computed(() => isAudit.value && props.state?.status === 'error' && !nodeAudit.value && !!unitCheckpoint.value?.audits?.[auditKind.value])
function sameJSON(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length && keys.every(key => Object.prototype.hasOwnProperty.call(right, key) && sameJSON(left[key], right[key]))
}
function sameAsNode(kind: 'deviation' | 'anti_ai') { return kind === auditKind.value && !!nodeAudit.value && sameJSON(nodeAudit.value, unitCheckpoint.value?.audits?.[kind]) }
function checkpointLabel(kind: 'deviation' | 'anti_ai') {
  const report = unitCheckpoint.value?.audits?.[kind]
  return report?.passed === false ? '未通过' : report?.passed === true ? '已记录通过结论' : '未完成'
}
function format(value: unknown) { return JSON.stringify(value, null, 2) }
</script>

<style scoped>
.setup-node-detail__body { display: grid; gap: 14px; overflow-wrap: anywhere; }
.setup-node-detail__summary { margin: 0; font-weight: 600; color: var(--app-text-primary); }
.setup-node-detail__meta,.setup-node-detail__section p { margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.7; }
.setup-node-detail__section { display: grid; gap: 10px; padding-top: 14px; border-top: 1px solid var(--app-border); }
.setup-node-detail__section h3 { margin: 0; font-size: 14px; color: var(--app-text-primary); }
.setup-node-detail__section strong { font-size: 12px; }
.setup-node-detail__section pre { max-height: 320px; overflow: auto; margin: 0; padding: 12px; border-radius: 6px; background: var(--app-surface); color: var(--app-text-secondary); font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.setup-node-detail__section summary { cursor: pointer; color: var(--app-text-secondary); font-size: 12px; padding: 6px 0; }
.setup-node-detail__checkpoint-audit { display: grid; gap: 8px; }
.setup-node-detail__replacement { display: grid; gap: 8px; }
:global(.setup-node-detail > .n-card-content) { min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
</style>
