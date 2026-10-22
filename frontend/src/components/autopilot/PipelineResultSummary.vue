<template>
  <section v-if="status?.pipeline_complete" role="status" aria-label="章节管线结果" class="pipeline-result">
    <strong>{{ resultLabel }}</strong>
    <span>{{ status.pipeline_success ? '正文已提交' : '正文流程未完成' }}</span>
    <ul v-if="pendingSteps.length">
      <li v-for="step in pendingSteps" :key="step.name">
        {{ step.label }}：{{ labels[step.result.status] }}<span v-if="step.result.message"> · {{ step.result.message }}</span>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { AutopilotStatus, PipelineResultStatus } from '@/api/autopilot'

const props = defineProps<{ status: AutopilotStatus | null | undefined }>()
const labels: Record<PipelineResultStatus, string> = {
  passed: '检查与派生已通过', failed: '检查失败', unavailable: '检查或派生不可用',
  skipped: '已跳过', deferred: '等待后续处理',
}
const stepLabels: Record<string, string> = {
  find_next_chapter: '章节定位', prepare_governance: '连续性检查', prepare_chapter_plan: '章节规划',
  build_context: '上下文', generate: '正文生成', validate_content: '内容检查', save_chapter: '正文保存',
  validate_voice: '文风检查', run_post_commit: '章后派生', score_tension: '张力评估', finalize: '收尾',
}
const resultLabel = computed(() => labels[props.status?.pipeline_status ?? 'skipped'])
const pendingSteps = computed(() => Object.entries(props.status?.pipeline_step_results ?? {})
  .filter(([, result]) => result.status !== 'passed')
  .map(([name, result]) => ({ name, label: stepLabels[name] ?? name, result })))
</script>

<style scoped>
.pipeline-result { padding: 12px; border: 1px solid var(--n-border-color, #888); border-radius: 8px; }
.pipeline-result > span { margin-left: 12px; }
.pipeline-result ul { margin: 8px 0 0; padding-left: 20px; }
</style>
