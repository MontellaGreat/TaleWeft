<template>
  <p v-if="progress && active" class="setup-model-call-status" role="status" aria-live="polite">
    <template v-if="progress.status === 'retry_wait'">{{ label }}：上游暂时未响应，{{ remaining > 0 ? `等待 ${remaining} 秒后自动重试` : '正在准备重试' }}（下一次 {{ progress.call_number + 1 }}/{{ progress.max_calls }}）。候选内容已保留，可停止生成。</template>
    <template v-else-if="reused">{{ label }}：复用已完成模型结果，继续校验。审核通过后才能保存。</template>
    <template v-else>{{ label }}：{{ receivedCharacters !== null && receivedCharacters > 0 ? '正在接收模型结果' : '等待模型响应' }}（{{ progress.call_number }}/{{ progress.max_calls }}）。<template v-if="transportDetails">{{ transportDetails }}。</template>收到完整结果后才会校验，审核通过后才能保存。</template>
  </p>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { SetupModelCallProgress } from '@/api/setupDag'
const props = defineProps<{ progress?: SetupModelCallProgress }>()
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null
const reused = computed(() => props.progress?.status === 'completed' && props.progress.reused === true)
const active = computed(() => ['streaming', 'retry_wait'].includes(props.progress?.status || '') || reused.value)
const label = computed(() => ({ deviation_review: '偏离检测', deviation_repair: '偏离修复', anti_ai_semantic_review: 'AI 味语义审稿', anti_ai_repair: 'AI 味修复' } as Record<string, string>)[props.progress?.purpose || ''] || '模型调用')
const remaining = computed(() => Math.max(0, Math.ceil(((props.progress?.retry_at || 0) * 1000 - now.value) / 1000)))
const elapsed = computed(() => {
  const startedAt = props.progress?.started_at
  return typeof startedAt === 'number' && Number.isFinite(startedAt)
    ? Math.max(0, Math.floor((now.value - startedAt * 1000) / 1000))
    : null
})
const receivedCharacters = computed(() => {
  const count = props.progress?.received_characters
  return typeof count === 'number' && Number.isFinite(count) ? Math.max(0, Math.floor(count)) : null
})
const transportDetails = computed(() => {
  const details: string[] = []
  if (elapsed.value !== null) details.push(`已等待 ${elapsed.value} 秒`)
  if (receivedCharacters.value !== null) details.push(`已收到 ${receivedCharacters.value} 字符`)
  const timeout = props.progress?.timeout_seconds
  if (typeof timeout === 'number' && Number.isFinite(timeout) && timeout > 0) details.push(`本次请求时限 ${timeout} 秒`)
  return details.join('，')
})
const stopTicker = () => {
  if (ticker !== null) clearInterval(ticker)
  ticker = null
}
watch([() => props.progress?.status, () => props.progress?.started_at], ([status]) => {
  stopTicker()
  now.value = Date.now()
  if (status === 'streaming' || status === 'retry_wait') ticker = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onBeforeUnmount(stopTicker)
</script>

<style scoped>
.setup-model-call-status { margin: 0; padding: 10px 12px; border: 1px solid var(--app-border); border-radius: 6px; color: var(--app-text-secondary); background: var(--app-surface); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
</style>
