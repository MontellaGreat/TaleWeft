<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NSpace, NSpin } from 'naive-ui'
import { aiInvocationApi, type InvocationResponseDTO } from '../../api/aiInvocation'
import { formatApiError } from '../../utils/apiError'
import { invocationFailureMessage } from '../../utils/invocationFailure'

const props = defineProps<{
  show: boolean
  novelId: string
  sessionId: string
  recoveryMessage?: string
}>()
const emit = defineEmits<{
  'update:show': [show: boolean]
  changed: []
}>()
const payload = ref<InvocationResponseDTO | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
let requestSequence = 0
let pollTimer: ReturnType<typeof setTimeout> | null = null
const sessionStatus = computed(() => String(payload.value?.session.status || ''))
const sessionLabels: Record<string, string> = {
  prompt_compiled: '上次请求中断，记录待核查', awaiting_pre_call_review: '等待批准生成',
  generating: '生成中', awaiting_acceptance: '等待采纳结果', awaiting_commit: '等待提交',
  committing: '提交中', completed: '已完成', blocked: '需要补齐资料', failed: '处理失败', cancelled: '已取消',
}
const statusLabel = computed(() => sessionLabels[sessionStatus.value] || '等待核查')
const generating = computed(() => ['generating', 'committing'].includes(sessionStatus.value))
const failure = computed(() => payload.value ? invocationFailureMessage(payload.value) : null)
const missing = computed(() => payload.value?.session.variable_plan?.required_missing
  || payload.value?.session.prompt_snapshot?.missing_variables || [])
const canRetry = computed(() => Boolean(payload.value?.session.prompt_snapshot?.prompt?.system
  || payload.value?.session.prompt_snapshot?.prompt?.user)
  && !payload.value?.decision?.id
  && ['prompt_compiled', 'failed', 'cancelled', 'awaiting_acceptance'].includes(sessionStatus.value))
const canAccept = computed(() => sessionStatus.value === 'awaiting_acceptance'
  && payload.value?.attempt?.status === 'succeeded' && !payload.value?.decision?.id)
const canCommit = computed(() => Boolean(payload.value?.decision?.id)
  && (sessionStatus.value === 'awaiting_commit' || payload.value?.commit?.status === 'failed'))

function stopPoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}
function schedulePoll() {
  stopPoll()
  if (props.show && generating.value) pollTimer = setTimeout(() => { void load() }, 2500)
}
function applyResponse(next: InvocationResponseDTO, resetDecision = false) {
  if (next.session?.id !== props.sessionId) throw new Error('会话已改变，请关闭后重新打开')
  const book = next.session.context?.novel_id
  if (book && book !== props.novelId) throw new Error('会话不属于当前作品')
  const previous = payload.value
  payload.value = { ...next,
    attempt: next.attempt ?? (resetDecision ? null : previous?.attempt ?? null),
    decision: next.decision ?? (resetDecision ? null : previous?.decision ?? null),
    commit: next.commit ?? (resetDecision ? null : previous?.commit ?? null),
  }
  schedulePoll()
}
async function load() {
  if (!props.show || !props.sessionId || busy.value) return
  const sequence = ++requestSequence
  loading.value = true
  error.value = ''
  try {
    const next = await aiInvocationApi.get(props.sessionId)
    if (sequence !== requestSequence || !props.show) return
    applyResponse(next)
  } catch (err) {
    if (sequence === requestSequence) error.value = formatApiError(err, '会话读取失败，请重试')
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}
async function act(action: 'retry' | 'resume' | 'accept' | 'commit') {
  if (busy.value || loading.value || !payload.value) return
  const sequence = ++requestSequence
  const sessionId = props.sessionId
  const current = payload.value
  stopPoll()
  busy.value = true
  error.value = ''
  try {
    let next: InvocationResponseDTO
    if (action === 'retry' && canRetry.value) {
      next = await aiInvocationApi.retry(sessionId, { resumed_by: 'user' })
    } else if (action === 'resume' && sessionStatus.value === 'awaiting_pre_call_review') {
      next = await aiInvocationApi.resume(sessionId, { resumed_by: 'user' })
    } else if (action === 'accept' && canAccept.value && current.attempt) {
      next = await aiInvocationApi.accept(sessionId, {
        attempt_id: current.attempt.id, accepted_by: 'user', commit_prompt_version: false,
      })
    } else if (action === 'commit' && canCommit.value && current.decision) {
      next = await aiInvocationApi.commit(sessionId, current.decision.id)
    } else return
    if (sequence !== requestSequence || !props.show) return
    applyResponse(next, action === 'retry' || action === 'resume')
    emit('changed')
  } catch (err) {
    if (sequence === requestSequence) error.value = formatApiError(err, 'AI 请求处理失败，请核对后重试')
  } finally {
    if (sequence === requestSequence) busy.value = false
  }
}
watch(() => [props.show, props.sessionId, props.novelId], () => {
  requestSequence++
  stopPoll()
  payload.value = null
  error.value = ''
  loading.value = false
  busy.value = false
  if (props.show) void load()
}, { immediate: true })
onBeforeUnmount(() => { requestSequence++; stopPoll() })
</script>

<template>
  <n-modal :show="show" preset="card" title="检查并恢复 AI 请求"
    style="width: min(760px, calc(100vw - 32px))" :mask-closable="!busy" :closable="!busy"
    @update:show="emit('update:show', $event)">
    <n-space vertical :size="14">
      <n-alert type="info" :show-icon="false">
        {{ recoveryMessage || '全托管已停止，已有 AI 会话尚未完成。打开此窗口只读取记录；下方操作由你逐步确认。' }}
      </n-alert>
      <n-alert v-if="error || failure" type="error" :show-icon="true">{{ error || failure }}</n-alert>
      <n-spin v-if="loading" size="small" />
      <template v-if="payload">
        <p class="recovery-status">请求状态：{{ statusLabel }} · 全托管未运行</p>
        <p v-if="sessionStatus === 'prompt_compiled'">上次调用中断，旧记录缺少调用回执。请检查原请求后，再决定是否重新请求。</p>
        <p v-else-if="generating">正在等待这次 AI 请求的处理结果；此窗口会刷新记录。</p>
        <p v-else-if="sessionStatus === 'completed'">会话已完成。恢复的宏观结构结果已保存，下次明确启动托管时会复用该结果写入结构；全托管仍需你明确启动。</p>
        <n-alert v-if="missing.length" type="warning">缺少资料：{{ missing.join('、') }}。请先补齐资料，再重新检查会话。</n-alert>
        <details v-if="payload.session.prompt_snapshot?.prompt">
          <summary>查看本次原始请求</summary>
          <pre class="recovery-content">{{ payload.session.prompt_snapshot.prompt.system }}

{{ payload.session.prompt_snapshot.prompt.user }}</pre>
        </details>
        <div v-if="payload.attempt?.content">
          <strong>已有生成结果</strong>
          <pre class="recovery-content">{{ payload.attempt.content }}</pre>
        </div>
        <ul v-if="payload.commit?.steps?.length">
          <li v-for="step in payload.commit.steps" :key="step.name">{{ step.name }}：{{ step.status }} {{ step.error || '' }}</li>
        </ul>
      </template>
      <n-space justify="end">
        <n-button :disabled="busy" @click="emit('update:show', false)">关闭</n-button>
        <n-button :disabled="busy || loading" @click="load">{{ payload ? '刷新会话' : '重试加载会话' }}</n-button>
        <n-button v-if="sessionStatus === 'awaiting_pre_call_review'" type="primary" :loading="busy" :disabled="loading" @click="act('resume')">批准生成</n-button>
        <n-button v-if="canRetry" type="warning" :loading="busy" :disabled="loading" @click="act('retry')">确认重新请求</n-button>
        <n-button v-if="canAccept" type="primary" :loading="busy" :disabled="loading" @click="act('accept')">采纳生成结果</n-button>
        <n-button v-if="canCommit" type="primary" :loading="busy" :disabled="loading" @click="act('commit')">提交已采纳结果</n-button>
      </n-space>
    </n-space>
  </n-modal>
</template>

<style scoped>
.recovery-status { margin: 0; color: var(--app-text-secondary); }
.recovery-content { max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--app-surface-subtle); border-radius: 6px; font-size: 12px; }
</style>
