<template>
  <details class="em-infer">
    <summary class="em-infer-summary">思考程度与推理参数 <span class="em-infer-arrow">⌄</span></summary>
    <div class="em-infer-body">
      <label v-if="protocol?.startsWith('openai')" class="em-infer-field em-thinking-field">
        <span>模型思考程度</span>
        <n-select :value="reasoningEffort" :options="reasoningOptions" clearable placeholder="由模型默认值决定" size="small" aria-label="模型思考程度" @update:value="emit('update:reasoningEffort', $event)" />
      </label>
      <label v-else class="em-infer-field em-thinking-field">
        <span>思考预算（token）</span>
        <n-input-number :value="thinkingBudget" :min="0" :step="1024" clearable placeholder="由模型默认值决定" size="small" aria-label="思考预算" @update:value="emit('update:thinkingBudget', $event)" />
      </label>
      <p class="em-infer-note">档位与预算以所选模型支持范围为准。留空采用模型默认；完整 AI 控制台中可检测能力与测试调用。</p>
      <label class="em-infer-field">
        <span>温度</span>
        <n-input-number
          class="em-infer-input"
          :value="temperature"
          :min="0"
          :max="2"
          :step="0.05"
          size="small"
          @update:value="onTemperature"
        />
      </label>
      <label class="em-infer-field">
        <span>最大 token</span>
        <n-input-number
          class="em-infer-input"
          :value="maxTokens"
          :min="1"
          :step="256"
          size="small"
          @update:value="onMaxTokens"
        />
      </label>
      <label class="em-infer-field">
        <span>超时（秒）</span>
        <n-input-number
          class="em-infer-input"
          :value="timeoutSeconds"
          :min="30"
          :max="3600"
          :step="10"
          size="small"
          @update:value="onTimeout"
        />
      </label>
    </div>
  </details>
</template>

<script setup lang="ts">
import { DEFAULT_MAX_OUTPUT_TOKENS } from '@/constants/llm'
import type { LLMProfile } from '@/api/llmControl'
type Effort = LLMProfile['reasoning_effort']
const reasoningOptions = [
  { label: '关闭 · none', value: 'none' }, { label: '最小 · minimal', value: 'minimal' },
  { label: '低 · low', value: 'low' }, { label: '中 · medium', value: 'medium' },
  { label: '高 · high', value: 'high' }, { label: '更高 · xhigh', value: 'xhigh' }, { label: '最大 · max', value: 'max' },
]

const props = defineProps<{
  temperature: number
  maxTokens: number
  timeoutSeconds: number
  protocol?: string
  reasoningEffort?: Effort
  thinkingBudget?: number | null
}>()

const emit = defineEmits<{
  'update:temperature': [number]
  'update:maxTokens': [number]
  'update:timeoutSeconds': [number]
  'update:reasoningEffort': [Effort]
  'update:thinkingBudget': [number | null]
}>()

function onTemperature(v: number | null) {
  emit('update:temperature', v ?? 0.7)
}

function onMaxTokens(v: number | null) {
  emit('update:maxTokens', Math.max(1, Math.floor(v ?? DEFAULT_MAX_OUTPUT_TOKENS)))
}

function onTimeout(v: number | null) {
  emit('update:timeoutSeconds', Math.max(30, Math.floor(v ?? props.timeoutSeconds)))
}
</script>

<style scoped>
.em-infer {
  margin-top: 10px;
  border-radius: 8px;
  border: 1px solid var(--app-border);
  background: var(--app-surface-subtle);
  overflow: hidden;
}
.em-thinking-field,.em-infer-note { grid-column:1 / -1; }
.em-infer-note { margin:0; font-size:11px; color:var(--app-text-muted); line-height:1.6; }
.em-infer-arrow { float:right; }

.em-infer-summary {
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--app-text-muted);
  cursor: pointer;
  list-style: none;
  user-select: none;
}

.em-infer-summary::-webkit-details-marker {
  display: none;
}

.em-infer[open] .em-infer-summary {
  border-bottom: 1px solid var(--app-border);
}

.em-infer-body {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  padding: 10px 12px;
}

.em-infer-field {
  display: grid;
  gap: 4px;
}

.em-infer-field span {
  font-size: 10px;
  color: var(--app-text-muted);
}

.em-infer-input {
  width: 100%;
}

@media (max-width: 640px) {
  .em-infer-body {
    grid-template-columns: 1fr;
  }
}
</style>
