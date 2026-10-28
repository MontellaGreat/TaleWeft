<template>
  <div class="setup-audit-report">
    <strong>{{ resultLabel }}</strong>
    <p v-if="typeof report.error === 'string'" class="setup-audit-report__error">{{ report.error }}</p>
    <template v-if="kind === 'deviation'">
      <dl class="setup-audit-report__checks">
        <div v-for="check in deviationChecks" :key="check.id"><dt>{{ check.label }}</dt><dd>{{ checkResult(check.id) }}</dd></div>
      </dl>
      <SetupReportList v-if="issues.length" :key="`${reportKey}-deviation`" :items="issues" label="偏离问题" :page-size="5"><template #default="{ item }"><p class="setup-audit-report__issue">{{ readable(item) }}</p></template></SetupReportList>
      <p v-else>{{ Array.isArray(report.issues) ? '报告未列出偏离问题。' : '报告缺少问题列表。' }}</p>
      <p v-if="report.evidence">依据：{{ readable(report.evidence) }}</p>
    </template>
    <template v-else>
      <section class="setup-audit-report__semantic" aria-label="模型语义审稿">
        <strong>模型语义审稿：{{ semanticLabel }}</strong>
        <p v-if="!semantic">缺少模型语义审稿；历史规则扫描结果不能代表完整文风审稿通过。</p>
        <template v-else>
          <p>检查空泛抒情、解释腔、机械结构、情绪标签和模板收尾；按故事正文、设定资料或文风规范分别审阅。</p>
          <p v-if="semantic.error" class="setup-audit-report__error">{{ semantic.error }}</p>
          <p v-if="semanticIssues.length">共 {{ semanticIssues.length }} 项文风问题</p>
          <SetupReportList v-if="semanticIssues.length" :key="`${reportKey}-semantic`" :items="semanticIssues" label="语义问题" :page-size="5">
            <template #default="{ item: issue }"><article class="setup-audit-report__issue">
              <strong>{{ categoryLabel(issue.category) }}</strong><span> · {{ issue.path }}</span>
              <blockquote>{{ issue.evidence }}</blockquote>
              <p>问题：{{ issue.reason }}</p><p>修改建议：{{ issue.suggestion }}</p>
            </article></template>
          </SetupReportList>
          <p v-else>{{ semanticComplete ? '模型本轮未列出需要修订的文风问题。' : '语义审稿尚无完整有效结论。' }}</p>
        </template>
      </section>
      <strong>本地规则扫描</strong>
      <p class="setup-audit-report__coverage">{{ leaves.length }} 个字段 · {{ completedLeaves.length }} 个完成 · {{ hitCountLabel }}<span v-if="ruleCountLabel"> · {{ ruleCountLabel }}</span></p>
      <p>扫描状态：{{ scanLabel(report.scan_status) }}</p>
      <div v-if="leaves.length" class="setup-audit-report__filters" role="group" aria-label="扫描字段筛选">
        <button type="button" aria-label="仅看有问题或未完成的扫描字段" :aria-pressed="!showAllFields" @click="showAllFields = false">仅看问题（{{ problemLeaves.length }}）</button>
        <button type="button" aria-label="查看全部扫描字段" :aria-pressed="showAllFields" @click="showAllFields = true">全部字段（{{ leaves.length }}）</button>
      </div>
      <p v-if="leaves.length && !showAllFields && !problemLeaves.length">本地规则未发现命中；可切换“全部字段”逐页查看扫描记录。语义审稿结论以上方报告为准。</p>
      <SetupReportList :key="`${reportKey}-${showAllFields}`" :items="visibleLeaves" label="扫描字段">
        <template #default="{ item: leaf }"><article class="setup-audit-report__leaf">
        <strong>字段：{{ leaf.path || '未标明字段' }}</strong>
        <p>命中 {{ typeof leaf.total_hits === 'number' ? leaf.total_hits : '数量未知' }} · 有效规则 {{ typeof leaf.effective_rule_count === 'number' ? leaf.effective_rule_count : '未记录' }} 条<span v-if="leaf.scan_status"> · {{ scanLabel(leaf.scan_status) }}</span></p>
        <SetupDetailDisclosure title="规则版本与文本校验"><p>规则版本：{{ leaf.rule_version || '暂无' }}</p><p>文本指纹：{{ leaf.content_hash || '暂无' }}；仅用于核对报告对应的内容。</p></SetupDetailDisclosure>
        <p v-if="leaf.error" class="setup-audit-report__error">{{ leaf.error }}</p>
        <p v-if="Array.isArray(leaf.instruction_exempted_hits) && leaf.instruction_exempted_hits.length">已保留 {{ leaf.instruction_exempted_hits.length }} 处文风禁用规则中的引用反例，未当作故事正文违规。</p>
        <SetupReportList v-if="Array.isArray(leaf.hits) && leaf.hits.length" :items="leaf.hits" label="规则命中" :page-size="5">
          <template #default="{ item: hit }"><article class="setup-audit-report__issue">
            <template v-if="isRecord(hit)">
              <strong>{{ hit.pattern || hit.pattern_name || hit.pattern_id || hit.rule_id || '命中条目' }}</strong><span v-if="hit.category"> · {{ hit.category }}</span><span v-if="hit.severity"> · {{ severityLabel(hit.severity) }}</span>
              <p v-if="hit.message || hit.description">{{ hit.message || hit.description }}</p>
              <blockquote v-if="hit.text || hit.matched_text">{{ hit.text || hit.matched_text }}</blockquote>
              <p v-if="hit.context">上下文：{{ readable(hit.context) }}</p>
              <p v-if="hit.start !== undefined || hit.position !== undefined">位置：{{ hit.start ?? hit.position }}<span v-if="hit.end !== undefined">–{{ hit.end }}</span></p>
              <p v-if="hit.replacement_hint || hit.suggestion">修改建议：{{ hit.replacement_hint || hit.suggestion }}</p>
            </template>
            <template v-else>{{ readable(hit) }}</template>
          </article></template>
        </SetupReportList>
        <p v-else>{{ Array.isArray(leaf.hits) && leaf.total_hits === 0 ? '该字段没有命中条目。' : '缺少可读取的命中条目。' }}</p>
      </article></template></SetupReportList>
      <p v-if="!leaves.length">暂无逐字段扫描记录，无法据此确认所有文本已扫描。</p>
    </template>
    <SetupDetailDisclosure :key="reportKey" class="setup-audit-report__raw" title="完整审核报告（原始证据）"><pre>{{ JSON.stringify(report, null, 2) }}</pre></SetupDetailDisclosure>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import SetupDetailDisclosure from './SetupDetailDisclosure.vue'
import SetupReportList from './SetupReportList.vue'
const props = defineProps<{ report: Record<string, unknown>; kind: 'deviation' | 'anti_ai' }>()
const showAllFields = ref(false)
const reportKey = computed(() => `${props.kind}:${props.report.scan_id || props.report.attempt_id || ''}:${props.report.input_hash || ''}`)
watch(reportKey, () => { showAllFields.value = false })
const semantic = computed(() => isRecord(props.report.semantic_review) ? props.report.semantic_review : null)
const semanticComplete = computed(() => {
  const value = semantic.value
  if (!value || value.status !== 'completed' || !['semantic-style-v1', 'semantic-style-v2'].includes(String(value.review_version)) || !value.attempt_id
    || typeof value.passed !== 'boolean' || !Array.isArray(value.issues)
    || !Array.isArray(value.coverage) || !value.coverage.length || !isRecord(value.checks)) return false
  return ['vague_lyricism', 'expository_voice', 'mechanical_structure', 'emotion_labeling', 'template_closure']
    .every(key => (value.checks as Record<string, unknown>)[key] === true) && value.passed === (value.issues.length === 0)
})
const semanticLabel = computed(() => !semantic.value ? '未记录' : !semanticComplete.value ? '未完成' : semantic.value.passed ? '未发现问题' : '发现问题')
const semanticIssues = computed(() => Array.isArray(semantic.value?.issues) ? semantic.value.issues.filter(isRecord) : [])
const resultLabel = computed(() => props.kind === 'anti_ai' && props.report.passed === true && (!semanticComplete.value || semantic.value?.passed !== true || !localComplete.value)
  ? '完整文风审稿尚未确认通过' : props.report.passed === true ? '报告结果：通过' : props.report.passed === false ? '报告结果：未通过' : '报告未给出有效通过结论')
const deviationChecks = [{ id: 'synopsis', label: '故事梗概' }, { id: 'genre', label: '题材与类型' }, { id: 'requirements', label: '作者要求' }, { id: 'predecessors', label: '前置设定' }]
function isRecord(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value) }
function checkResult(key: string) { const checks = props.report.checks; const value = isRecord(checks) ? checks[key] : undefined; return value === true ? '符合' : value === false ? '不符合' : '未记录' }
const issues = computed(() => Array.isArray(props.report.issues) ? props.report.issues : [])
const leaves = computed(() => Array.isArray(props.report.reports) ? props.report.reports.filter(isRecord) : [])
function leafComplete(leaf: Record<string, unknown>) {
  return leaf.scan_status === 'completed' && !leaf.error && typeof leaf.effective_rule_count === 'number' && Number.isInteger(leaf.effective_rule_count) && leaf.effective_rule_count > 0
    && typeof leaf.rule_version === 'string' && leaf.rule_version.length > 0
    && typeof leaf.total_hits === 'number' && Number.isInteger(leaf.total_hits) && leaf.total_hits >= 0
    && Array.isArray(leaf.hits) && leaf.total_hits === leaf.hits.length
}
const completedLeaves = computed(() => leaves.value.filter(leafComplete))
const problemLeaves = computed(() => leaves.value.filter(leaf => !leafComplete(leaf) || leaf.total_hits !== 0))
const localComplete = computed(() => props.report.scan_status === 'completed' && leaves.value.length > 0 && !problemLeaves.value.length)
const visibleLeaves = computed(() => showAllFields.value ? leaves.value : problemLeaves.value)
const hitCountLabel = computed(() => leaves.value.length && leaves.value.every(leaf => typeof leaf.total_hits === 'number' && Number.isInteger(leaf.total_hits) && leaf.total_hits >= 0)
  ? `${leaves.value.reduce((total, leaf) => total + Number(leaf.total_hits), 0)} 处命中` : '命中数未完整记录')
const ruleCountLabel = computed(() => {
  const counts = new Set(leaves.value.map(leaf => leaf.effective_rule_count))
  return leaves.value.length && counts.size === 1 && typeof leaves.value[0]?.effective_rule_count === 'number'
    ? `每字段 ${leaves.value[0].effective_rule_count} 条规则` : ''
})
function readable(value: unknown): string {
  if (typeof value === 'string') return value
  if (isRecord(value)) {
    const description = value.description || value.message || value.issue || value.reason
    if (typeof description === 'string') return description
  }
  return JSON.stringify(value)
}
function scanLabel(status: unknown) { return ({ completed: '已完成', failed: '失败', skipped: '未执行', unavailable: '不可用', invalid: '无效', coverage_empty: '有效规则为空，未通过' } as Record<string, string>)[String(status)] || String(status || '未记录') }
function categoryLabel(value: unknown) { return ({ vague_lyricism: '空泛抒情', expository_voice: '解释腔', mechanical_structure: '机械结构', emotion_labeling: '情绪标签', template_closure: '模板收尾' } as Record<string, string>)[String(value)] || String(value || '文风问题') }
function severityLabel(value: unknown) { return ({ critical: '严重', warning: '警告', info: '提示' } as Record<string, string>)[String(value)] || String(value) }
</script>

<style scoped>
.setup-audit-report { display: grid; gap: 8px; font-size: 12px; line-height: 1.7; }
.setup-audit-report p,.setup-audit-report ul { margin: 0; }
.setup-audit-report__checks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
.setup-audit-report__checks div { display: flex; justify-content: space-between; gap: 12px; padding: 6px 10px; background: var(--app-surface); border-radius: 5px; }
.setup-audit-report__checks dd { margin: 0; }
.setup-audit-report__leaf { padding: 10px; border: 1px solid var(--app-border); border-radius: 6px; }
.setup-audit-report blockquote { margin: 4px 0; padding-left: 10px; border-left: 2px solid var(--app-border); }
.setup-audit-report__semantic { display: grid; gap: 8px; padding: 12px; background: var(--app-surface); border-radius: 6px; }
.setup-audit-report summary { cursor: pointer; color: var(--app-text-muted); }
.setup-audit-report pre { max-height: 320px; overflow: auto; padding: 12px; background: var(--app-surface); border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.setup-audit-report__error { color: var(--color-danger); }
.setup-audit-report__coverage { font-variant-numeric: tabular-nums; }
.setup-audit-report__issue { padding: 8px 0; border-bottom: 1px solid var(--app-border); }
.setup-audit-report__filters { display: flex; flex-wrap: wrap; gap: 8px; }
.setup-audit-report__filters button { cursor: pointer; border: 1px solid var(--app-border); padding: 6px 10px; border-radius: 5px; background: var(--app-surface); color: var(--app-text-primary); font: inherit; touch-action: manipulation; }
.setup-audit-report__filters button[aria-pressed="true"],.setup-audit-report__filters button:hover { border-color: var(--app-accent, #527765); }
.setup-audit-report__filters button:focus-visible { outline: 2px solid var(--app-accent, #527765); outline-offset: 2px; }
@media (max-width: 600px) { .setup-audit-report__checks { grid-template-columns: 1fr; } }
</style>
