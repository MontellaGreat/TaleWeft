<template>
  <div class="autopilot-panel">
    <section class="ap-hero" aria-label="运行状态">
      <div class="ap-hero__top">
        <div class="ap-hero__status">
          <span class="ap-dot" :class="dotClass" aria-hidden="true" />
          <span class="ap-hero__eyebrow">守护进程</span>
          <span class="ap-stage-tag" :class="stageTagClass">
            <template v-if="stageTransitioning">
              <span class="skeleton-inline skeleton-pulse" />
              <span class="stage-transition-label">
                <span class="stage-text">{{ stagePresentation.text }}</span>
                <span v-if="stagePresentation.live" class="ap-stage-live" aria-label="实时同步" />
              </span>
            </template>
            <template v-else>
              <span class="stage-text">{{ stagePresentation.text }}</span>
              <span v-if="stagePresentation.live" class="ap-stage-live" aria-label="实时同步" />
            </template>
          </span>
          <span
            v-if="isWriting"
            class="ap-sse-pill"
            :class="sseConnected ? 'is-on' : 'is-off'"
          >
            {{ sseConnected ? '流式已连接' : (sseReconnecting ? '重连中' : '流式未连接') }}
          </span>
        </div>
        <div class="ap-hero__pct" :class="{ 'is-active': isRunning }">
          <span class="ap-hero__pct-value">{{ progressPctDisplay }}</span>
          <span class="ap-hero__pct-label">全书进度</span>
        </div>
      </div>
      <n-progress
        class="ap-hero__bar"
        type="line"
        :percentage="progressPct"
        :color="progressColor"
        :show-indicator="false"
        :height="8"
        :border-radius="4"
      />
      <p v-if="status" class="ap-hero__plan-line">
        目标篇幅（与首页一致）
        <strong>{{ formatWords(planTotalWordsHint) }}</strong> 字 ·
        <strong>{{ status.target_chapters ?? '—' }}</strong> 章 ×
        <strong>{{ status.target_words_per_chapter ?? 2500 }}</strong> 字/章
        <n-button
          text
          type="primary"
          size="tiny"
          class="ap-hero__plan-toggle"
          @click="planExpanded = !planExpanded"
        >
          {{ planExpanded ? '收起说明' : '说明' }}
        </n-button>
      </p>
      <p v-if="status && planExpanded" class="ap-plan-detail">
        写满目标章即停；导演剧本按章节大纲生成，正文按剧本一次撰写。流式字数可能暂时高于章目标。
        进度条、幕/章与阶段标签可能短暂不同步，以守护进程状态为准。
      </p>
    </section>

    <n-alert
      v-if="statusConnectivityFailures >= 2 && !statusPollDisabled"
      type="warning"
      :show-icon="true"
      class="ap-inline-alert"
    >
      无法连接写作后端。已自动拉长轮询间隔，请确认桌面后端或开发 API 已启动后再试。
    </n-alert>

    <n-alert v-if="controlPending" type="info" :show-icon="true" class="ap-inline-alert">
      {{ controlPending.control_message || (controlPending.command_status === 'unknown' ? '命令提交结果尚未确认，正在查询，请勿重复操作。' : (controlPending.command_type === 'stop' ? '停止请求已提交，等待当前任务退出。' : '启动请求已提交，等待执行器确认。')) }}
    </n-alert>
    <n-alert v-else-if="status?.control_message" type="warning" :show-icon="true" class="ap-inline-alert">
      {{ status.control_message }}
    </n-alert>

    <section v-if="status" class="ap-kpi-grid" aria-label="关键指标">
      <article class="ap-kpi">
        <span class="ap-kpi__label">完稿 / 书稿 / 目标</span>
        <span class="ap-kpi__value">
          {{ status.completed_chapters || 0 }}
          <span class="ap-kpi__sep">/</span>
          {{ status.manuscript_chapters ?? status.completed_chapters ?? 0 }}
          <span class="ap-kpi__sep">/</span>
          {{ status.target_chapters || '—' }}
        </span>
      </article>
      <article class="ap-kpi">
        <span class="ap-kpi__label">总字数</span>
        <span class="ap-kpi__value">{{ formatWords(status.total_words) }}</span>
      </article>
      <article class="ap-kpi ap-kpi--location">
        <span class="ap-kpi__label">当前位置</span>
        <span class="ap-location">
          <span class="ap-location__meta">第 {{ (status.current_act || 0) + 1 }} 幕</span>
          <span class="ap-location__title">{{ status.current_act_title || '当前幕' }}</span>
          <!-- 规划阶段：显示阶段标签 -->
          <span class="ap-location__trail">
            <span v-if="!isWriting && status.current_stage === 'act_planning'" class="ap-location__chip">
              幕级规划
            </span>
            <span v-else-if="!isWriting && status.current_stage === 'macro_planning'" class="ap-location__chip">
              宏观规划
            </span>
            <!-- 撰写阶段：只有 writing_substep 激活后才显示章/阶段，避免展示上一章的残留状态 -->
            <span v-if="isWriting && status.current_chapter_number != null && status.writing_substep" class="ap-location__chip ap-location__chip--strong">
              第 {{ status.current_chapter_number }} 章
            </span>
            <span v-if="isWriting && stageIndicator" class="ap-location__chip">{{ stageIndicator }}</span>
          </span>
        </span>
      </article>
      <article class="ap-kpi">
        <span class="ap-kpi__label">上章张力</span>
        <span class="ap-kpi__value" :style="{ color: tensionColor }">{{ tensionLabel }}</span>
      </article>
    </section>

    <section
      v-if="status?.current_act_description || (status?.current_act_title && !status?.current_act_description)"
      class="ap-narrative"
      aria-label="当前幕叙事"
    >
      <span class="ap-narrative__label">当前幕</span>
      <p v-if="status.current_act_description" class="ap-narrative__body">
        <span v-if="status.current_act_title" class="ap-narrative__title">{{ status.current_act_title }}</span>
        {{ status.current_act_description }}
      </p>
      <p v-else class="ap-narrative__body ap-narrative__body--muted">暂无幕描述</p>
    </section>

    <section
      v-if="telemetryVisible"
      class="ap-telemetry"
      aria-label="实时子步骤"
    >
      <header class="ap-telemetry__head">
        <span class="ap-telemetry__title">实时管线</span>
        <span class="substep-badge" :class="substepBadgeClass">{{ writingSubstepDetail.substepLabel }}</span>
      </header>
      <div class="ap-telemetry__grid">
        <div v-if="writingSubstepDetail.accumulatedWords > 0" class="ap-telemetry__item">
          <span class="ap-telemetry__key">本章字数</span>
          <span class="ap-telemetry__val">
            {{ writingSubstepDetail.accumulatedWords }}/{{ writingSubstepDetail.chapterTargetWords }}
            <span class="pct-tag">{{ writingSubstepDetail.wordPct }}%</span>
          </span>
          <div class="ap-meter">
            <div
              class="ap-meter__fill ap-meter__fill--word"
              :style="{ width: Math.min(100, writingSubstepDetail.wordPct) + '%' }"
            />
          </div>
        </div>
        <div v-if="writingSubstepDetail.contextTokens > 0" class="ap-telemetry__item">
          <span class="ap-telemetry__key">上下文</span>
          <span class="ap-telemetry__val">{{ writingSubstepDetail.contextTokens }} tokens</span>
        </div>
      </div>
    </section>

    <PipelineResultSummary :status="status" />

    <StoryPipelineObservability
      v-if="storyPipelineObsVisible"
      :status="status"
      :aftermath-only="storyPipelineAftermathOnly"
    />

    <AuditPipelineObservability
      v-if="auditPipelineObsVisible"
      :status="status"
    />

    <!-- 单本挂起 / 失败计数过高 -->
    <n-alert v-if="needsRecovery" type="error" :show-icon="true" class="ap-inline-alert">
      <div class="recovery-hint">
        <p v-if="status?.autopilot_status === 'error'">
          本书已因<strong>连续失败</strong>被标为<strong>异常挂起</strong>。
        </p>
        <p v-else>
          已连续失败 <strong>{{ status?.consecutive_error_count || 0 }}</strong> 次（达到 3 次会挂起）。
        </p>
        <p class="recovery-sub">
          全局 LLM 熔断在守护进程内，无法在此直接展示。下方按钮与「监控大盘 → 熔断保护 → 重置」相同。
        </p>
        <n-button
          size="small"
          type="primary"
          secondary
          :loading="toggling"
          @click="clearCircuitBreaker"
        >
          解除挂起并清零计数
        </n-button>
      </div>
    </n-alert>

    <ContinuationStructureReview
      v-if="activeContinuation"
      :novel-id="props.novelId"
      :proposal="activeContinuation"
      @refresh="loadContinuationProposals"
      @changed="refreshAfterContinuation"
    />
    <p v-if="continuationLoadError" role="alert">{{ continuationLoadError }} <n-button text @click="loadContinuationProposals">重试加载续写状态</n-button></p>

    <n-alert v-if="hasStoppedInvocation" type="warning" :show-icon="true" class="ap-inline-alert">
      <div class="ap-review-alert">
        <span>{{ status?.invocation_recovery_message || '全托管已停止，已有 AI 请求尚未完成，可检查记录后手动恢复。' }}</span>
        <n-button size="small" type="warning" @click="openInvocationRecovery">检查并恢复 AI 请求</n-button>
      </div>
    </n-alert>
    <AutopilotInvocationRecovery
      v-model:show="invocationRecoveryVisible"
      :novel-id="props.novelId || ''"
      :session-id="invocationRecoverySessionId"
      :recovery-message="status?.invocation_recovery_message"
      @changed="fetchStatus"
    />

    <!-- 审阅等待 -->
    <n-alert v-if="showReviewGate && !awaitingContinuationStructure" :type="reviewGateAlertType" :show-icon="true" class="ap-inline-alert">
      <div class="ap-review-alert">
        <span>
          <strong>{{ reviewGateTitle }}</strong>：{{ reviewGateMessage }}
        </span>
        <n-button
          v-if="reviewGateNeedsAIPanel && featureFlags.aiInvocationDebug"
          type="warning"
          size="small"
          :loading="aiPanelOpening"
          @click="() => openActiveInvocation()"
        >
          打开 AI 面板
        </n-button>
        <n-button
          v-else-if="canResumeReview"
          type="warning"
          size="small"
          :loading="toggling"
          @click="resume"
        >
          {{ reviewGateActionLabel }}
        </n-button>
      </div>
    </n-alert>

    <!-- 仅写作阶段拉章节流；审计/规划时服务端会关流，避免无意义重连 -->
    <AutopilotWritingStream
      v-if="(isWriting || hasUncommittedPreview) && props.renderLivePreview !== false"
      :writing-content="writingContent"
      :writing-chapter-number="writingChapterNumber"
      :writing-substep="status?.writing_substep"
      :writing-substep-label="status?.writing_substep_label"
      :accumulated-words="status?.accumulated_words"
      :chapter-target-words="status?.chapter_target_words"
      :context-tokens="status?.context_tokens"
      :runner-stage-label="stageLabel"
      :status-chapter-number="status?.current_chapter_number ?? null"
      :is-writing-phase="isWriting"
      :uncommitted-preview="hasUncommittedPreview"
    />

    <!-- 操作按钮 -->
    <n-space justify="end" size="small">
      <n-button v-if="canResumeReview" type="warning" ghost size="small" :loading="toggling" @click="resume">
        再次确认 · 继续
      </n-button>
      <n-button v-if="isCompleted" type="warning" ghost size="small" :loading="continuationBusy" @click="openContinuationModal">
        提出续写提案
      </n-button>
      <n-button v-if="!isRunning && !isCompleted && !needsReview && !needsRecovery" type="primary" size="small" :loading="toggling" @click="openStartModal">
        🚀 启动全托管
      </n-button>
      <n-button v-if="isRunning" type="error" ghost size="small" :loading="toggling" @click="stop">
        ⏹ 停止
      </n-button>
      <!-- 🔥 error 状态下显示强制停止按钮（解除挂起 + 停止） -->
      <n-button v-if="needsRecovery && !isRunning" type="error" size="small" :loading="toggling" @click="forceStopFromError">
        ⏹ 强制停止
      </n-button>
    </n-space>

    <n-modal v-model:show="showContinuationModal" preset="card" title="完成后的续写 / 重规划" style="width: min(560px, calc(100vw - 32px))">
      <n-space vertical :size="12" style="width: 100%">
        <n-alert type="info" :show-icon="false">
          已完成章节会保留。系统先生成提案，审批后进入宏观重规划，作者确认新增结构后才会继续写作。
        </n-alert>
        <n-form v-if="!continuationProposal">
          <n-form-item label="新的目标章数">
            <n-input-number v-model:value="continuationTarget" :min="(status?.target_chapters || 0) + 1" :max="9999" :step="5" style="width: 100%" />
          </n-form-item>
          <n-form-item label="提案类型">
            <n-radio-group v-model:value="continuationMode">
              <n-radio value="continue">续写后续章节</n-radio>
              <n-radio value="replan">重规划后续结构</n-radio>
            </n-radio-group>
          </n-form-item>
          <n-form-item label="提案备注">
            <n-input v-model:value="continuationRationale" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" placeholder="可选：说明希望补足的剧情方向" />
          </n-form-item>
        </n-form>
        <template v-else>
          <n-alert type="success" :show-icon="false">
            提案已生成：从第 {{ continuationProposal.from_chapter }} 章开始，扩展 {{ continuationProposal.plan?.additional_chapters || 0 }} 章，目标 {{ continuationProposal.proposed_target_chapters }} 章。
          </n-alert>
          <ol v-if="continuationProposal.plan?.steps?.length" class="continuation-steps">
            <li v-for="step in continuationProposal.plan.steps" :key="step">{{ step }}</li>
          </ol>
        </template>
        <n-space justify="end">
          <n-button @click="showContinuationModal = false">关闭</n-button>
          <n-button v-if="continuationProposal" tertiary type="error" :loading="continuationBusy" @click="rejectContinuationProposal">放弃提案</n-button>
          <n-button v-if="!continuationProposal" type="primary" :loading="continuationBusy" @click="createContinuationProposal">生成提案</n-button>
          <n-button v-else type="warning" :loading="continuationBusy" @click="approveContinuationProposal">采纳提案并进入规划</n-button>
        </n-space>
      </n-space>
    </n-modal>

    <!-- 启动配置弹窗 -->
    <n-modal v-model:show="showStartModal" title="启动全托管" preset="dialog" positive-text="启动" @positive-click="start">
      <n-space vertical :size="12" style="width: 100%">
        <n-alert type="success" :show-icon="true" style="font-size: 12px">
          <strong>自动托管</strong>：配置好参数后点击"启动"。执行器确认后开始自动写作，可在此查看启动状态与执行进度。
        </n-alert>
        <n-form>
          <n-form-item label="目标章数">
            <n-input-number
              v-model:value="startConfig.target_chapters"
              :min="1"
              :max="9999"
              :step="10"
              style="width: 100%"
              @update:value="updateProtectionLimit"
            />
          </n-form-item>
          <n-form-item label="每章目标字数">
            <n-input-number
              v-model:value="startConfig.target_words_per_chapter"
              :min="500"
              :max="20000"
              :step="500"
              style="width: 100%"
            />
          </n-form-item>
          <n-form-item label="保护上限（章节数，防止意外消耗）">
            <n-input-number
              v-model:value="startConfig.max_auto_chapters"
              :min="startConfig.target_chapters"
              :max="9999"
              :step="10"
              style="width: 100%"
            />
          </n-form-item>

          <n-form-item label="全自动模式">
            <n-space align="center" justify="space-between" style="width: 100%">
              <n-switch
                v-model:value="startConfig.auto_approve_mode"
                :round="false"
              >
                <template #checked>开启</template>
                <template #unchecked>关闭</template>
              </n-switch>
              <n-text depth="3" style="font-size: 12px">
                自动处理普通写作审阅
              </n-text>
            </n-space>
          </n-form-item>

          <n-alert type="info" :show-icon="false" style="font-size: 11px; margin-top: -8px">
            <template v-if="startConfig.auto_approve_mode">
              <strong>全自动模式已开启</strong>：普通写作审阅自动处理。续写结构仍须作者明确确认。
            </template>
            <template v-else>
              达到 <strong>{{ startConfig.target_chapters }} 章</strong> 目标时自动完成全书。
            </template>
          </n-alert>
        </n-form>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted, watch } from 'vue'
import { useMessage } from 'naive-ui'
import AutopilotWritingStream from './AutopilotWritingStream.vue'
import StoryPipelineObservability from './StoryPipelineObservability.vue'
import PipelineResultSummary from './PipelineResultSummary.vue'
import AuditPipelineObservability from './AuditPipelineObservability.vue'
import ContinuationStructureReview from './ContinuationStructureReview.vue'
import AutopilotInvocationRecovery from './AutopilotInvocationRecovery.vue'
import { chapterApi } from '../../api/chapter'
import {
  autopilotApi,
  getAutopilotErrorDetail,
  isAutopilotHttpError,
  isAutopilotNotFoundError,
} from '../../api/autopilot'
import { novelApi } from '../../api/novel'
import { buildAutopilotStagePresentation } from '../../constants/autopilotStagePresentation'
import { useAIInvocationStore } from '../../stores/aiInvocationStore'
import { featureFlags } from '../../config/features'
import { runtimePerformance } from '../../config/performance'

const props = defineProps({
  novelId: String,
  renderLivePreview: { type: Boolean, default: true },
})
const emit = defineEmits([
  'status-change',
  'chapter-content-update',
  'chapter-start',
  'chapter-chunk',
  'desk-refresh',
  'beats-planned',
])
const message = useMessage()
const aiInvocationStore = useAIInvocationStore()
const panelPerformance = runtimePerformance.autopilotPanel

const status = ref(null)
const toggling = ref(false)
const controlPending = ref(null)
const heartbeatNow = ref(Date.now())
const heartbeatClock = setInterval(() => { heartbeatNow.value = Date.now() }, 1000)
const aiPanelOpening = ref(false)
const invocationRecoveryVisible = ref(false)
const invocationRecoverySessionId = ref('')
const planExpanded = ref(false)
const showStartModal = ref(false)
const showContinuationModal = ref(false)
const continuationBusy = ref(false)
const continuationTarget = ref(0)
const continuationMode = ref('continue')
const continuationRationale = ref('')
const continuationProposal = ref(null)
const continuationProposals = ref([])
const continuationLoadError = ref('')
const continuationLoadedNovelId = ref('')
let continuationLoadInFlight = false
let continuationLoadSeq = 0
const activeContinuation = computed(() => continuationProposals.value.find(proposal =>
  ['approved', 'macro_planning', 'awaiting_structure_review'].includes(proposal.status)
) || (isCompleted.value ? continuationProposals.value.find(proposal => proposal.status === 'legacy_unverified') : null))
const awaitingContinuationStructure = computed(() => activeContinuation.value?.status === 'awaiting_structure_review')
const startConfig = ref({
  target_chapters: 100,
  target_words_per_chapter: 2500,
  max_auto_chapters: 120,
  auto_approve_mode: true
})

// 🔧 新增：SSE 连接状态
const sseConnected = ref(false)
const sseReconnecting = ref(false)
let chapterStreamCtrl = null
let reconnectTimer = null
let reconnectAttempts = 0
/** 递增后忽略旧连接的 onDisconnected / onStreamEnd，避免 stop→abort 与重连竞态 */
let chapterStreamSession = 0
let lastChapterStreamStartMs = 0
const MAX_RECONNECT_ATTEMPTS = panelPerformance.maxChapterStreamReconnectAttempts
const MIN_CHAPTER_STREAM_RESTART_MS = panelPerformance.minChapterStreamRestartMs
const HEARTBEAT_GRACE_SECONDS = panelPerformance.daemonHeartbeatGraceSeconds
const STAGE_TRANSITION_DELAY_MS = panelPerformance.stageTransitionDelayMs
const CHAPTER_STREAM_CONTENT_MAX_LENGTH = panelPerformance.chapterStreamContentMaxLength
const STREAM_RETRY_COOLDOWN_MULTIPLIER = panelPerformance.streamRetryCooldownMultiplier

// 写作内容状态
const writingContent = ref('')
const writingChapterNumber = ref(0)
const writingBeatIndex = ref(0)
let activePreviewRunId = ''
let chapterChunkEmitTimer = null
let pendingChapterChunk = null
const CHAPTER_CHUNK_EMIT_INTERVAL_MS = panelPerformance.chapterChunkEmitIntervalMs

function flushChapterChunkEmit() {
  if (chapterChunkEmitTimer) {
    clearTimeout(chapterChunkEmitTimer)
    chapterChunkEmitTimer = null
  }
  if (!pendingChapterChunk) return
  emit('chapter-chunk', pendingChapterChunk)
  pendingChapterChunk = null
}

function emitChapterChunkThrottled(payload, immediate = false) {
  pendingChapterChunk = payload
  if (immediate) {
    flushChapterChunkEmit()
    return
  }
  if (chapterChunkEmitTimer) return
  chapterChunkEmitTimer = setTimeout(flushChapterChunkEmit, CHAPTER_CHUNK_EMIT_INTERVAL_MS)
}

// 🔥 新增：操作节流保护——防止用户快速连续点击导致请求堆积
// toggling 为 true 时按钮已禁用，但需要额外保护异步操作的竞态
let lastToggleTime = 0
const TOGGLE_THROTTLE_MS = panelPerformance.toggleThrottleMs

function isToggleThrottled() {
  const now = Date.now()
  if (now - lastToggleTime < TOGGLE_THROTTLE_MS) {
    return true
  }
  lastToggleTime = now
  return false
}

// 状态轮询
let statusPollTimer = null
let statusPollDisposed = false
const statusPollDisabled = ref(false)
// /status：新请求开始前取消上一轮，减轻后端堆积；序号用于忽略已被替代的 AbortError
let statusFetchSeq = 0
let statusLastAbort = null
/** 连续无法拉取 /status（网络拒绝/超时）时倍增轮询间隔 */
const statusConnectivityFailures = ref(0)
let lastStatusPollIntervalMs = -1

// 计算属性
const isRunning = computed(() => status.value?.autopilot_status === 'running')
const isTerminalStopped = computed(() =>
  ['stopped', 'completed'].includes(String(status.value?.autopilot_status || ''))
)
const hasStoppedInvocation = computed(() => {
  const s = status.value
  if (!s || !['stopped', 'error'].includes(String(s.autopilot_status || ''))) return false
  if (!String(s.active_invocation_session_id || '').trim()) return false
  const state = String(s.active_invocation_status || '')
  if (state === 'completed') return false
  return Boolean(s.invocation_recovery_required || s.requires_ai_review || s.has_active_invocation)
})
function openInvocationRecovery() {
  if (!hasStoppedInvocation.value) return
  invocationRecoverySessionId.value = String(status.value?.active_invocation_session_id || '')
  invocationRecoveryVisible.value = true
}
defineExpose({ openInvocationRecovery })
const isCompleted = computed(() =>
  String(status.value?.current_stage || '') === 'completed' ||
  String(status.value?.autopilot_status || '') === 'completed'
)
// 是否与人工审阅闸门对齐（须点 resume）。
// 「reviewing」为兼容舞台值；主路径 paused_for_review。避免仅展示「待审阅」却无按钮。
function statusNeedsManualReview(s) {
  if (!s) return false
  if (['stopped', 'completed'].includes(String(s.autopilot_status || ''))) return false
  if (s.needs_review === true) return true
  const stage = String(s.current_stage ?? '').trim().toLowerCase()
  return stage === 'paused_for_review' || stage === 'reviewing'
}

const needsReview = computed(() => statusNeedsManualReview(status.value))
const requiresAIReview = computed(() => Boolean(
  !isTerminalStopped.value && status.value?.requires_ai_review && status.value?.active_invocation_session_id
))
const reviewGate = computed(() => {
  const gate = status.value?.review_gate
  return gate && typeof gate === 'object' ? gate : null
})
const reviewGateType = computed(() => String(reviewGate.value?.type || 'manual_review'))
const reviewGateStatus = computed(() => String(reviewGate.value?.status || 'ready'))
const reviewGateNeedsAIPanel = computed(() =>
  !isTerminalStopped.value && (reviewGate.value?.primary_action === 'open_ai_panel' || requiresAIReview.value)
)
const showReviewGate = computed(() => needsReview.value || reviewGateNeedsAIPanel.value)
const canResumeReview = computed(() => (
  needsReview.value &&
  !awaitingContinuationStructure.value &&
  !continuationLoadError.value &&
  continuationLoadedNovelId.value === props.novelId &&
  !requiresAIReview.value &&
  (!reviewGate.value || reviewGateStatus.value === 'ready') &&
  reviewGate.value?.can_resume !== false
))
const reviewGateAlertType = computed(() => (
  reviewGateStatus.value === 'failed' ? 'error' : 'warning'
))
const reviewGateTitle = computed(() => {
  if (reviewGateStatus.value === 'failed') {
    if (reviewGateType.value === 'macro_plan') return '宏观结构生成失败'
    if (reviewGateType.value === 'act_plan') return '章节规划生成失败'
    return 'AI 请求处理失败'
  }
  if (reviewGateStatus.value === 'awaiting_ai_review') return '等待 AI 请求处理'
  if (reviewGateStatus.value === 'persisting') return '正在生成大纲结构'
  if (reviewGateType.value === 'macro_plan') return '待确认宏观结构'
  if (reviewGateType.value === 'act_plan') return '待确认章节规划'
  if (reviewGateType.value === 'chapter_review') return '待确认章节审阅'
  return '待审阅确认'
})
const reviewGateMessage = computed(() => {
  if (reviewGate.value?.message) return reviewGate.value.message
  if (requiresAIReview.value) {
    return `${activeInvocationLabel.value} 已发送到统一 AI 面板，请完成生成、采纳和提交。`
  }
  return '请在侧栏核对刚生成的结构或审阅结果，核对无误后继续。'
})
const reviewGateActionLabel = computed(() => (
  reviewGate.value?.action_label || '确认后继续'
))
function statusHasActiveInvocation(s) {
  return Boolean(s?.active_invocation_session_id && (s?.has_active_invocation || s?.requires_ai_review))
}
const activeInvocationLabel = computed(() => {
  const op = status.value?.active_invocation_operation || 'AI 请求'
  const node = status.value?.active_invocation_node_key || ''
  return node ? `${op} / ${node}` : op
})
// 🔥 只有运行中且阶段为 writing 时才是真正的"撰写中"
const isWriting = computed(() =>
  status.value?.autopilot_status === 'running' && status.value?.current_stage === 'writing'
)

const hasUncommittedPreview = computed(() =>
  Boolean(
    writingContent.value &&
    !isWriting.value &&
    (
      status.value?.autopilot_status === 'stopped' ||
      status.value?.writing_substep === 'interrupted' ||
      status.value?.autopilot_recovery_reason === 'retry_writing_step'
    )
  )
)

const storyPipelineWaveIndex = computed(() => {
  const ix = Number(status.value?.story_pipeline_wave_index)
  return Number.isFinite(ix) ? ix : 0
})

const storyPipelineAftermathOnly = computed(() =>
  isWriting.value && storyPipelineWaveIndex.value === 8
)

const telemetryVisible = computed(() =>
  isRunning.value &&
  Boolean(writingSubstepDetail.value) &&
  !storyPipelineAftermathOnly.value &&
  !auditPipelineObsVisible.value
)

/** StoryPipeline（新内核写作）有可观测字段时展示十步管线图 */
const storyPipelineObsVisible = computed(() => {
  if (auditPipelineObsVisible.value) return false
  if (!isWriting.value || !status.value) return false
  const ix = storyPipelineWaveIndex.value
  return Number.isFinite(ix) && ix >= 1 && ix <= 10
})

const auditPipelineObsVisible = computed(() => {
  if (!isRunning.value || !status.value) return false
  const stage = String(status.value.current_stage || '')
  const sub = String(status.value.writing_substep || '')
  const audit = String(status.value.audit_progress || '')
  return (
    stage === 'auditing' ||
    sub === 'pipeline_done' ||
    sub.startsWith('audit_') ||
    audit === 'voice_check' ||
    audit === 'aftermath_pipeline' ||
    audit === 'tension_scoring'
  )
})
const needsRecovery = computed(
  () =>
    status.value?.autopilot_status === 'error' ||
    (status.value?.consecutive_error_count || 0) >= 3
)
// Shared state survives executor crashes; only a current heartbeat proves life.
const daemonAlive = computed(() => {
  const heartbeat = Number(status.value?.daemon_heartbeat_at)
  const lease = Number(status.value?.daemon_lease_seconds || 60)
  const age = heartbeatNow.value / 1000 - heartbeat
  return heartbeat > 0 && age >= 0 && age < Math.min(lease, HEARTBEAT_GRACE_SECONDS)
})

const targetChapters = computed(() => status.value?.target_chapters || 100)

const planTotalWordsHint = computed(() => {
  const s = status.value
  if (!s) return 0
  if (s.target_plan_total_words != null && s.target_plan_total_words > 0) {
    return s.target_plan_total_words
  }
  return (s.target_chapters ?? 0) * (s.target_words_per_chapter ?? 2500)
})

const progressPct = computed(() => {
  const s = status.value
  if (!s) return 0
  const target = Number(s.target_chapters || 0)
  const completed = Number(s.completed_chapters || 0)
  const manuscript = Number(s.manuscript_chapters ?? completed)
  const currentAuto = Number(s.current_auto_chapters || 0)
  const bestCount = Math.max(completed, manuscript, currentAuto)
  if (target > 0 && bestCount > 0) {
    return Math.min(100, Math.round((bestCount / target) * 1000) / 10)
  }
  const serverPct = Number(s.progress_pct_manuscript ?? s.progress_pct ?? 0)
  return Number.isFinite(serverPct) ? serverPct : 0
})

const progressPctDisplay = computed(() => {
  const n = Number(progressPct.value)
  if (!Number.isFinite(n)) return '0%'
  return `${n < 10 ? n.toFixed(1) : Math.round(n * 10) / 10}%`
})

const progressColor = computed(() => {
  if (needsRecovery.value) return 'var(--color-danger, #ef4444)'
  if (needsReview.value) return 'var(--color-warning, #f59e0b)'
  return 'var(--color-success, #22c55e)'
})

const dotClass = computed(() => ({
  'dot-running': isRunning.value && !needsReview.value,
  'dot-review': needsReview.value,
  'dot-error': status.value?.autopilot_status === 'error',
  'dot-stopped': !isRunning.value && !needsReview.value,
}))

const stagePresentation = computed(() =>
  buildAutopilotStagePresentation({
    current_stage: status.value?.current_stage,
    autopilot_status: status.value?.autopilot_status,
    writing_substep: status.value?.writing_substep,
    writing_substep_label: status.value?.writing_substep_label,
    active_pipeline_step: status.value?.active_pipeline_step,
    autopilot_recovery_reason: status.value?.autopilot_recovery_reason,
    _from_shared_memory: status.value?._from_shared_memory,
    _degraded: status.value?._degraded,
    audit_progress: status.value?.audit_progress,
    isRunning: isRunning.value,
    daemonAlive: daemonAlive.value,
    current_act: status.value?.current_act ?? null,
  })
)

const stageLabel = computed(() => stagePresentation.value.text)

// 🔥 阶段变更过渡态：检测 current_stage 变化时显示骨架 loading
const prevStage = ref(null)
const stageTransitioning = ref(false)
let stageTransitionTimer = null

function clearStageTransitionTimer() {
  if (stageTransitionTimer) {
    clearTimeout(stageTransitionTimer)
    stageTransitionTimer = null
  }
}

watch(
  () => status.value?.current_stage,
  (newStage, oldStage) => {
    if (oldStage && newStage && oldStage !== newStage) {
      // 阶段变了，触发骨架 loading 过渡
      stageTransitioning.value = true
      clearStageTransitionTimer()
      stageTransitionTimer = setTimeout(() => {
        stageTransitioning.value = false
      }, STAGE_TRANSITION_DELAY_MS)
    }
    prevStage.value = newStage
  }
)

const stageTagClass = computed(() => {
  const sem = stagePresentation.value.semantic
  const run = isRunning.value && !needsReview.value
  return {
    'tag-review': needsReview.value,
    'tag-idle': !isRunning.value && !needsReview.value,
    'tag-transitioning': stageTransitioning.value,
    'tag-sem-plan': run && sem === 'plan',
    'tag-sem-write': run && sem === 'write',
    'tag-sem-audit': run && sem === 'audit',
    'tag-sem-sync': run && sem === 'sync',
    'tag-sem-review': run && sem === 'review',
    'tag-sem-idle': run && sem === 'idle',
    'tag-sem-daemon_wait': run && sem === 'daemon_wait',
  }
})

const stageIndicator = computed(() => {
  if (!isWriting.value) return ''
  const sub = status.value?.writing_substep || ''
  if (sub === 'script_generation') return '剧本生成中'
  if (sub === 'prose_generation') return '正文撰写中'
  return ''
})

/** ★ V9 细化状态：写作/审计/规划子步骤详情 */
const writingSubstepDetail = computed(() => {
  if (!status.value) return null
  const s = status.value
  const substep = String(s.writing_substep || '')
  const substepLabel = String(s.writing_substep_label || '')
  if (!substep && !substepLabel) return null

  const accumulatedWords = Number(s.accumulated_words || 0)
  const chapterTargetWords = Number(s.chapter_target_words || 0)
  const wordPct = chapterTargetWords > 0 && accumulatedWords > 0
    ? Math.min(100, Math.round(accumulatedWords / chapterTargetWords * 100))
    : 0

  return {
    substep,
    substepLabel: substepLabel || substep,
    accumulatedWords,
    chapterTargetWords,
    wordPct,
    contextTokens: Number(s.context_tokens || 0),
  }
})

/** 子步骤徽章配色 */
const substepBadgeClass = computed(() => {
  const sub = status.value?.writing_substep || ''
  // 写作阶段
  if (sub === 'prose_generation') return 'substep-active'
  if (sub === 'outline_planning') return 'substep-plan'
  if (sub === 'context_assembly' || sub === 'script_generation' || sub === 'chapter_found') return 'substep-prepare'
  if (sub === 'persisting' || sub === 'continuity_check' || sub === 'chapter_persist') return 'substep-finish'
  // 审计阶段
  if (sub === 'audit_voice_check') return 'substep-audit'
  if (sub === 'audit_aftermath') return 'substep-audit'
  if (sub === 'audit_tension') return 'substep-audit'
  // 规划阶段
  if (sub === 'macro_planning') return 'substep-plan'
  if (sub === 'act_planning') return 'substep-plan'
  return ''
})

const tensionLabel = computed(() => {
  // 张力值范围是 0-100，转换为 0-10 显示
  const rawT = status.value?.last_chapter_tension || 0
  if (rawT < 0) return `⏳ 未评估`
  const t = Math.round(rawT / 10) // 0-100 转 0-10
  if (t >= 8) return `🔥 高潮 (${t}/10)`
  if (t >= 6) return `⚡ 冲突 (${t}/10)`
  if (t >= 4) return `🌊 暗流 (${t}/10)`
  return `💤 平缓 (${t}/10)`
})

const tensionColor = computed(() => {
  // 张力值范围是 0-100，转换为 0-10 判断
  const rawT = status.value?.last_chapter_tension || 0
  if (rawT < 0) return '#999'
  const t = Math.round(rawT / 10)
  return t >= 8 ? '#d03050' : t >= 6 ? '#f0a020' : t >= 4 ? '#18a058' : '#36ad6a'
})

// 格式化
function formatWords(n) {
  if (!n) return '0'
  return n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(n)
}

// 后端 /status 已改为纯共享内存读取；超时由运行性能配置统一管理。
const STATUS_FETCH_TIMEOUT_MS = panelPerformance.statusFetchTimeoutMs

// 🔥 新增：请求去重——如果上一次 fetchStatus 还没返回，不重复发起
let statusFetchInFlight = false
let lastOpenedInvocationSessionId = ''
let openingInvocationSessionId = ''

async function fetchStatus() {
  // 请求去重：上一次还在飞就不重复发
  if (statusFetchInFlight) return

  statusFetchSeq += 1
  const seq = statusFetchSeq
  const requestNovelId = props.novelId
  if (statusLastAbort) {
    statusLastAbort.abort()
  }
  const ac = new AbortController()
  statusLastAbort = ac
  statusFetchInFlight = true
  try {
    const body = await autopilotApi.getStatus(requestNovelId, {
      signal: ac.signal,
      timeoutMs: STATUS_FETCH_TIMEOUT_MS,
    })
    if (seq !== statusFetchSeq || requestNovelId !== props.novelId || statusPollDisposed) return
    if (!controlPending.value && body.command_id && ['pending', 'applied'].includes(body.command_status)
        && !body.execution_reservation_expired) {
      controlPending.value = { ...body }
    }
    if (controlPending.value?.command_id) {
      let receipt = body
      if (body.command_id !== controlPending.value.command_id) {
        try {
          receipt = await autopilotApi.getCommand(requestNovelId, controlPending.value.command_id, { signal: ac.signal, timeoutMs: STATUS_FETCH_TIMEOUT_MS })
        } catch (err) {
          // An unconfirmed submission may still commit after a request timeout.
          receipt = null
        }
      }
      if (seq !== statusFetchSeq || requestNovelId !== props.novelId || statusPollDisposed) return
      if (receipt) reconcileControlReceipt(receipt)
    }
    statusConnectivityFailures.value = 0
    reconcilePipelineRun(body)
    status.value = body
    void loadContinuationProposals()
    emit('status-change', body)
    maybeOpenActiveInvocation(body)

    // 🔍 调试：审计阶段进度日志
    if (body.current_stage === 'auditing') {
      console.log(
        '[AutopilotPanel] 审计进度:',
        body.audit_progress || '(未知)',
        '| 相似度:', body.last_chapter_audit?.similarity_score ?? 'N/A',
        '| 张力:', body.last_chapter_tension ?? 'N/A'
      )
    }

    // 写作阶段流掉线且已放弃重连：由轮询在冷却后再试（勿在此处清零 reconnectAttempts，否则会死循环）
    if (
      shouldMaintainChapterStream(body) &&
      !chapterStreamCtrl &&
      !sseReconnecting.value &&
      reconnectAttempts >= MAX_RECONNECT_ATTEMPTS &&
      Date.now() - lastChapterStreamStartMs >= MIN_CHAPTER_STREAM_RESTART_MS * STREAM_RETRY_COOLDOWN_MULTIPLIER
    ) {
      reconnectAttempts = MAX_RECONNECT_ATTEMPTS - 1
      scheduleChapterStreamReconnect(0)
    }
  } catch (err) {
    if (seq !== statusFetchSeq) {
      return
    }
    if (isAutopilotNotFoundError(err)) {
      clearStatusPoll()
      status.value = null
      statusPollDisabled.value = true
      statusConnectivityFailures.value = 0
      return
    }
    statusConnectivityFailures.value += 1
    if (err instanceof Error && err.name === 'AbortError') {
      console.warn('[AutopilotPanel] fetchStatus 超时，可能后端繁忙或未启动')
    } else {
      console.error('[AutopilotPanel] fetchStatus error:', err)
    }
  } finally {
    if (seq === statusFetchSeq) {
      statusFetchInFlight = false
      maybeRestartStatusPollTimer()
    }
  }
}

function resetWritingPreview() {
  writingContent.value = ''
  writingChapterNumber.value = 0
  writingBeatIndex.value = 0
  pendingChapterChunk = null
  flushChapterChunkEmit()
}

function reconcilePipelineRun(body) {
  const nextRunId = String(body?.active_pipeline_run_id || '').trim()
  if (nextRunId && activePreviewRunId && nextRunId !== activePreviewRunId) {
    resetWritingPreview()
  }
  if (nextRunId) {
    activePreviewRunId = nextRunId
  }
}

function resolveActiveInvocationSessionId(sessionIdArg) {
  if (typeof sessionIdArg === 'string' && sessionIdArg.trim()) {
    return sessionIdArg.trim()
  }
  return String(status.value?.active_invocation_session_id || '').trim()
}

async function openActiveInvocation(sessionIdArg, options = {}) {
  const sessionId = resolveActiveInvocationSessionId(sessionIdArg)
  if (!sessionId) return
  if (sessionId === openingInvocationSessionId) return
  const showPanel = options.showPanel !== false
  openingInvocationSessionId = sessionId
  if (showPanel) {
    aiPanelOpening.value = true
  }
  try {
    await aiInvocationStore.open(sessionId, { showPanel })
    lastOpenedInvocationSessionId = sessionId
  } catch (err) {
    console.warn('[AutopilotPanel] 打开 AI Invocation 面板失败:', err)
    message.error('AI 调用处理失败')
  } finally {
    openingInvocationSessionId = ''
    if (showPanel) {
      aiPanelOpening.value = false
    }
  }
}

function maybeOpenActiveInvocation(s) {
  // Opening the store can advance a headless session. A stopped book only
  // offers the explicit recovery window, whose initial request is read-only.
  if (s?.autopilot_status !== 'running' || s?.active_invocation_status === 'completed') return
  const sessionId = String(s?.active_invocation_session_id || '')
  if (!sessionId) return
  if (sessionId === lastOpenedInvocationSessionId) return
  if (sessionId === openingInvocationSessionId) return
  void openActiveInvocation(sessionId, { showPanel: featureFlags.aiInvocationDebug })
}

function clearStatusPoll() {
  if (statusPollTimer) {
    clearTimeout(statusPollTimer)
    statusPollTimer = null
  }
  lastStatusPollIntervalMs = -1
}

/** 轮询间隔变化时（如后端断连退避）重置 timer，避免固定 3～5s 刷满 Vite 代理日志 */
function maybeRestartStatusPollTimer() {
  if (statusPollDisposed || statusPollDisabled.value) return
  const ms = getAdaptivePollInterval()
  if (statusPollTimer != null && ms === lastStatusPollIntervalMs) {
    return
  }
  lastStatusPollIntervalMs = ms
  if (statusPollTimer) {
    clearTimeout(statusPollTimer)
    statusPollTimer = null
  }
  statusPollTimer = setTimeout(() => {
    statusPollTimer = null
    void fetchStatus()
  }, ms)
}

/** 章节正文 SSE 仅在「运行中 + 写作阶段」需要；审计/规划时服务端会关流，不应重连 */
function shouldMaintainChapterStream(body = status.value) {
  if (!body || statusPollDisabled.value) return false
  if (body.autopilot_status !== 'running') return false
  if (statusHasActiveInvocation(body)) return false
  if (statusNeedsManualReview(body)) return false
  return body.current_stage === 'writing'
}

function wantsChapterStream() {
  return shouldMaintainChapterStream()
}

function scheduleChapterStreamReconnect(delayMs) {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (!shouldMaintainChapterStream()) {
    sseReconnecting.value = false
    return
  }
  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    sseReconnecting.value = false
    return
  }
  const delay = Math.max(delayMs, MIN_CHAPTER_STREAM_RESTART_MS)
  reconnectAttempts++
  sseReconnecting.value = true
  console.log(
    `[AutopilotPanel] SSE 断开，${delay / 1000}s 后重连 (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`,
  )
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    void fetchStatus().then(() => {
      if (!shouldMaintainChapterStream()) {
        sseReconnecting.value = false
        reconnectAttempts = 0
        return
      }
      if (!chapterStreamCtrl && !sseConnected.value) {
        startChapterStream()
      }
    })
  }, delay)
}

function startChapterStream() {
  if (!shouldMaintainChapterStream()) {
    stopChapterStream()
    return
  }

  const now = Date.now()
  if (now - lastChapterStreamStartMs < MIN_CHAPTER_STREAM_RESTART_MS && chapterStreamCtrl) {
    return
  }

  stopChapterStream()
  const session = chapterStreamSession
  lastChapterStreamStartMs = now
  sseReconnecting.value = true

  console.log('[AutopilotPanel] 启动 SSE 连接...')

  chapterStreamCtrl = chapterApi.subscribeStream(props.novelId, {
    onOutlinePlanning: () => {
      if (session !== chapterStreamSession) return
      void fetchStatus()
    },
    onBeatsPlanned: (chapterNumber, beats) => {
      if (session !== chapterStreamSession) return
      void fetchStatus()
      emit('desk-refresh')
      emit('beats-planned', { chapterNumber, beats })
    },
    onChapterStart: (num) => {
      if (session !== chapterStreamSession) return
      const isNewChapter = writingChapterNumber.value !== num
      writingChapterNumber.value = num
      // SSE 重连会对同一章再次发 chapter_start，勿清空已累积正文
      if (isNewChapter) {
        writingContent.value = ''
        writingBeatIndex.value = 0
      }
      reconnectAttempts = 0  // 重置重连计数
      emit('chapter-start', num)
      // 🔥 新章节开始写时刷新侧栏，让结构树/章节列表同步（规划后首次写作尤其需要）
      emit('desk-refresh')
    },
    onChapterChunk: (payload) => {
      if (session !== chapterStreamSession) return
      const maxLen = CHAPTER_STREAM_CONTENT_MAX_LENGTH
      if (payload.isSnapshot && payload.content != null) {
        if (payload.content.length <= maxLen) {
          writingContent.value = payload.content
        }
      } else if (payload.chunk && writingContent.value.length < maxLen) {
        writingContent.value += payload.chunk
      }
      writingBeatIndex.value = payload.beatIndex
      emitChapterChunkThrottled({
        chunk: payload.chunk ?? '',
        beatIndex: payload.beatIndex,
        content: writingContent.value,
        chapterNumber: writingChapterNumber.value,
        isSnapshot: payload.isSnapshot,
      }, Boolean(payload.isSnapshot))
    },
    onChapterContent: (data) => {
      if (session !== chapterStreamSession) return
      writingContent.value = data.content
      writingChapterNumber.value = data.chapterNumber
      writingBeatIndex.value = data.beatIndex
      emit('chapter-content-update', data)
    },
    onAutopilotStopped: () => {
      if (session !== chapterStreamSession) return
      reconnectAttempts = 0
      void fetchStatus()
      // 🔥 全书完成/停止时刷新章节列表，确保侧栏「已收稿」状态同步
      emit('desk-refresh')
    },
    onPausedForReview: () => {
      if (session !== chapterStreamSession) return
      reconnectAttempts = 0
      void fetchStatus()
      // 🔥 进入待审阅时刷新章节列表和结构树
      emit('desk-refresh')
    },
    onConnected: () => {
      if (session !== chapterStreamSession) return
      sseConnected.value = true
      sseReconnecting.value = false
      console.log('[AutopilotPanel] SSE 已连接')
    },
    onStreamEnd: (reason) => {
      if (session !== chapterStreamSession) return
      sseConnected.value = false
      chapterStreamCtrl = null
      sseReconnecting.value = false
      void fetchStatus().then(() => {
        if (reason === 'stopped' || reason === 'review') {
          reconnectAttempts = 0
          return
        }
        // 服务端在非写作阶段关流（idle）：仅当仍处于 writing 时才重连
        if (!shouldMaintainChapterStream()) {
          reconnectAttempts = 0
          return
        }
        scheduleChapterStreamReconnect(panelPerformance.streamIdleReconnectDelayMs)
      })
    },
    onDisconnected: () => {
      if (session !== chapterStreamSession) return
      sseConnected.value = false
      chapterStreamCtrl = null
      void fetchStatus().then(() => {
        if (!shouldMaintainChapterStream()) {
          sseReconnecting.value = false
          reconnectAttempts = 0
          return
        }
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
          console.warn('[AutopilotPanel] SSE 重连次数过多，暂停章节流（仍可通过 /status 轮询看进度）')
          sseReconnecting.value = false
          return
        }
        const delay = Math.min(
          panelPerformance.streamReconnectBaseDelayMs * 2 ** Math.max(reconnectAttempts - 1, 0),
          panelPerformance.streamReconnectMaxDelayMs,
        )
        scheduleChapterStreamReconnect(delay)
      })
    },
    onError: (err) => {
      if (session !== chapterStreamSession) return
      sseConnected.value = false
      console.error('[AutopilotPanel] SSE 错误:', err)
    },
  })
}

function stopChapterStream() {
  chapterStreamSession++
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (chapterStreamCtrl) {
    chapterStreamCtrl.abort()
    chapterStreamCtrl = null
  }
  sseConnected.value = false
  sseReconnecting.value = false
}

// 🔧 优化：自适应状态轮询 + SSE 协同
// 策略：
// - SSE 已连接时：降频兜底（SSE 已实时驱动刷新，轮询仅防断连漏检）
// - SSE 未连接但运行中：较高频补偿 SSE 缺失
// - 非运行中：用户可能刚操作，需要快速看到状态变化
// - 审阅等待中：用户在看大纲，不需要高频刷新
function getAdaptivePollInterval() {
  let base
  if (requiresAIReview.value) base = panelPerformance.pollRequiresAiReviewMs
  else if (needsReview.value) base = panelPerformance.pollManualReviewMs
  else if (!isRunning.value) base = panelPerformance.pollIdleMs
  else if (sseConnected.value) base = panelPerformance.pollSseConnectedMs
  else base = panelPerformance.pollRunningMs
  const mult = Math.min(2 ** Math.min(statusConnectivityFailures.value, 8), 128)
  return Math.min(base * mult, panelPerformance.pollMaxMs)
}

watch(
  [
    () => isRunning.value,
    () => requiresAIReview.value,
    () => needsReview.value,
    () => statusPollDisabled.value,
    () => status.value?.current_stage,
  ],
  () => {
    clearStatusPoll()
    if (statusPollDisabled.value) return

    lastStatusPollIntervalMs = -1
    maybeRestartStatusPollTimer()
    void fetchStatus()

    if (wantsChapterStream()) {
      if (!chapterStreamCtrl && !sseReconnecting.value) {
        startChapterStream()
      }
    } else {
      stopChapterStream()
      reconnectAttempts = 0
    }
  },
  { immediate: true }
)

// 🔥 SSE 连接状态变化时仅调整轮询间隔，不重新管理 SSE 连接（避免与 onDisconnected 双重重连）
watch(
  () => sseConnected.value,
  () => {
    if (!statusPollDisabled.value) {
      lastStatusPollIntervalMs = -1
      maybeRestartStatusPollTimer()
    }
  }
)

watch(
  () => props.novelId,
  () => {
    statusFetchSeq += 1
    statusLastAbort?.abort()
    statusFetchInFlight = false
    status.value = null
    controlPending.value = null
    continuationLoadSeq += 1
    continuationLoadInFlight = false
    continuationProposals.value = []
    continuationLoadError.value = ''
    continuationLoadedNovelId.value = ''
    continuationProposal.value = null
    showContinuationModal.value = false
    invocationRecoveryVisible.value = false
    invocationRecoverySessionId.value = ''
    toggling.value = false
    statusPollDisabled.value = false
    statusConnectivityFailures.value = 0
    reconnectAttempts = 0
    lastOpenedInvocationSessionId = ''
    openingInvocationSessionId = ''
    activePreviewRunId = ''
    resetWritingPreview()
    stopChapterStream()
    void fetchStatus()
  }
)

function openStartModal() {
  const target = status.value?.target_chapters || 100
  const wpc = status.value?.target_words_per_chapter ?? 2500
  const autoApprove = status.value?.auto_approve_mode ?? false
  startConfig.value = {
    target_chapters: target,
    target_words_per_chapter: wpc,
    max_auto_chapters: target + 20,
    auto_approve_mode: autoApprove
  }
  showStartModal.value = true
}

function openContinuationModal() {
  continuationTarget.value = Number(status.value?.target_chapters || 0) + 5
  continuationMode.value = 'continue'
  continuationRationale.value = ''
  continuationProposal.value = continuationProposals.value.find(proposal => proposal.status === 'proposed') || null
  showContinuationModal.value = true
}

async function loadContinuationProposals() {
  if (continuationLoadInFlight || !props.novelId || statusPollDisposed) return
  if (!['completed', 'macro_planning', 'paused_for_review', 'reviewing'].includes(status.value?.current_stage) && !activeContinuation.value) return
  const seq = ++continuationLoadSeq, novelId = props.novelId
  continuationLoadInFlight = true
  try {
    const proposals = await autopilotApi.listContinuationProposals(novelId)
    if (seq !== continuationLoadSeq || novelId !== props.novelId || statusPollDisposed) return
    continuationProposals.value = proposals
    continuationLoadError.value = ''
    continuationLoadedNovelId.value = novelId
  } catch (err) {
    if (seq === continuationLoadSeq && novelId === props.novelId && !statusPollDisposed)
      continuationLoadError.value = getAutopilotErrorDetail(err) || '续写状态加载失败，请刷新后核对'
  } finally { if (seq === continuationLoadSeq) continuationLoadInFlight = false }
}

async function refreshAfterContinuation() {
  await loadContinuationProposals()
  await fetchStatus()
  emit('desk-refresh')
}

async function createContinuationProposal() {
  if (continuationBusy.value) return
  continuationBusy.value = true
  try {
    continuationProposal.value = await autopilotApi.createContinuationProposal(props.novelId, {
      target_chapters: Number(continuationTarget.value),
      mode: continuationMode.value,
      rationale: continuationRationale.value,
    })
    message.success('续写提案已生成，请核对后采纳')
  } catch (err) {
    message.error(getAutopilotErrorDetail(err) || '续写提案生成失败')
  } finally {
    continuationBusy.value = false
  }
}

async function approveContinuationProposal() {
  const proposal = continuationProposal.value
  if (!proposal || continuationBusy.value) return
  continuationBusy.value = true
  try {
    const body = await autopilotApi.approveContinuationProposal(props.novelId, proposal.proposal_id)
    acceptControlResponse(body, 'resume', props.novelId)
    showContinuationModal.value = false
    await fetchStatus()
  } catch (err) {
    message.error(getAutopilotErrorDetail(err) || '续写提案审批失败')
  } finally {
    continuationBusy.value = false
  }
}

async function rejectContinuationProposal() {
  const proposal = continuationProposal.value
  if (!proposal || continuationBusy.value) return
  continuationBusy.value = true
  try {
    await autopilotApi.rejectContinuationProposal(props.novelId, proposal.proposal_id)
    continuationProposal.value = null
    message.info('提案已放弃')
    await refreshAfterContinuation()
  } catch (err) {
    message.error(getAutopilotErrorDetail(err) || '提案放弃失败')
  } finally {
    continuationBusy.value = false
  }
}

function updateProtectionLimit() {
  const target = startConfig.value.target_chapters
  if (startConfig.value.max_auto_chapters < target + 20) {
    startConfig.value.max_auto_chapters = target + 20
  }
}

function reconcileControlReceipt(body) {
  const pending = controlPending.value
  if (!pending || body.command_id !== pending.command_id) return
  if (body.command_status === 'completed' && body.execution_confirmed === true) {
    message.success(pending.command_type === 'stop' ? '执行器已确认停止' : '执行器已确认启动')
    controlPending.value = null
  } else if (['failed', 'superseded'].includes(body.command_status)
      || (body.execution_reservation_expired && ['start', 'resume'].includes(pending.command_type))) {
    message.warning(body.control_message || body.command_error || (body.command_status === 'superseded' ? '该命令已被后续操作替代' : '执行器未能执行该命令，请重试启动'))
    controlPending.value = null
  } else {
    controlPending.value = { ...pending, ...body }
  }
}

function acceptControlResponse(body, type, novelId) {
  if (novelId !== props.novelId || statusPollDisposed) return
  if (body.no_op) {
    message.info(body.message || '本书已完成')
    return
  }
  if (!body.command_id) throw new Error('控制接口缺少命令回执')
  controlPending.value = { ...body, command_type: type, requestedLocallyAt: Date.now() }
  if (body.execution_confirmed === true) reconcileControlReceipt(body)
  else if (body.accepted === true) message.info(body.message || '命令已提交，等待执行器确认')
  else message.warning(body.message || '提交结果尚未确认，正在查询')
}

async function start() {
  if (controlPending.value || isToggleThrottled()) return
  const novelId = props.novelId
  toggling.value = true
  try {
    const config = { ...startConfig.value }
    if ((status.value?.auto_approve_mode ?? false) !== config.auto_approve_mode) {
      await novelApi.updateAutoApproveMode(novelId, config.auto_approve_mode)
    }
    const response = await autopilotApi.start(novelId, {
      max_auto_chapters: config.max_auto_chapters,
      target_chapters: config.target_chapters,
      target_words_per_chapter: config.target_words_per_chapter,
    })
    acceptControlResponse(await response.json(), 'start', novelId)
    if (novelId === props.novelId) showStartModal.value = false
    await fetchStatus()
  } catch (err) {
    if (novelId === props.novelId) message.error(getAutopilotErrorDetail(err) || '启动请求未确认，请查询状态后再重试')
  } finally {
    if (novelId === props.novelId) toggling.value = false
  }
}

async function stop() {
  if (controlPending.value?.command_type === 'stop' || isToggleThrottled()) return
  const novelId = props.novelId
  toggling.value = true
  try {
    const response = await autopilotApi.stop(novelId, panelPerformance.stopRequestTimeoutMs)
    acceptControlResponse(await response.json(), 'stop', novelId)
    await fetchStatus()
  } catch (err) {
    if (novelId !== props.novelId) return
    message.warning(getAutopilotErrorDetail(err) || '停止请求结果尚未确认，请观察执行器状态后再重试')
    await fetchStatus()
  } finally {
    if (novelId === props.novelId) toggling.value = false
  }
}

async function resume() {
  if (controlPending.value || isToggleThrottled()) return
  if (!canResumeReview.value) {
    message.warning(reviewGateMessage.value || '当前还没有可确认的产物')
    return
  }
  const novelId = props.novelId
  toggling.value = true
  try {
    const body = await autopilotApi.resume(novelId)
    acceptControlResponse(body, 'resume', novelId)
    await fetchStatus()
  } catch (err) {
    if (novelId === props.novelId) message.error(getAutopilotErrorDetail(err) || '恢复请求尚未确认，请查询状态后再重试')
  } finally {
    if (novelId === props.novelId) toggling.value = false
  }
}

async function clearCircuitBreaker() {
  if (controlPending.value || isToggleThrottled()) return
  toggling.value = true
  try {
    await autopilotApi.resetCircuitBreaker(props.novelId)
    message.success('失败计数已清零')
    await fetchStatus()
  } catch (err) {
    message.error(getAutopilotErrorDetail(err) || '解除挂起失败，请重试')
  } finally {
    toggling.value = false
  }
}

async function forceStopFromError() {
  await stop()
}

onUnmounted(() => {
  clearInterval(heartbeatClock)
  statusPollDisposed = true
  flushChapterChunkEmit()
  statusFetchSeq += 1
  statusFetchInFlight = false  // 🔥 重置请求去重标志
  clearStageTransitionTimer()
  if (statusLastAbort) {
    statusLastAbort.abort()
    statusLastAbort = null
  }
  clearStatusPoll()
  stopChapterStream()
})
</script>

<style scoped>
.autopilot-panel {
  --ap-accent: var(--color-success, #22c55e);
  --ap-card-bg: var(--app-surface-raised, var(--app-surface));
  --ap-card-border: var(--app-border);
  background: var(--ap-card-bg);
  border: 1px solid var(--ap-card-border);
  border-radius: var(--app-radius-lg, 14px);
  padding: 16px 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-shadow: var(--app-shadow-md);
}

.ap-hero {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border-radius: var(--app-radius-md, 10px);
  background: linear-gradient(
    145deg,
    color-mix(in srgb, var(--color-primary, #2563eb) 5%, var(--app-surface-subtle)) 0%,
    var(--app-surface-subtle) 55%
  );
  border: 1px solid var(--app-border);
}

.ap-hero__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.ap-hero__status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
  flex: 1 1 200px;
}

.ap-hero__eyebrow {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--app-text-muted);
}

.ap-hero__pct {
  text-align: right;
  flex-shrink: 0;
}

.ap-hero__pct-value {
  display: block;
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--app-text-primary);
  letter-spacing: -0.03em;
}

.ap-hero__pct.is-active .ap-hero__pct-value {
  color: var(--ap-accent);
}

.ap-hero__pct-label {
  font-size: 10px;
  color: var(--app-text-muted);
  margin-top: 2px;
}

.ap-hero__bar :deep(.n-progress-graph) {
  border-radius: 4px;
}

.ap-hero__plan-line {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--app-text-secondary);
}

.ap-hero__plan-line strong {
  color: var(--app-text-primary);
  font-weight: 600;
}

.ap-hero__plan-toggle {
  margin-left: 4px;
  vertical-align: baseline;
}

.ap-plan-detail {
  margin: 0;
  padding: 10px 12px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--app-text-muted);
  background: color-mix(in srgb, var(--app-text-primary) 3%, transparent);
  border-radius: var(--app-radius-sm, 8px);
  border-left: 3px solid var(--color-primary, #2563eb);
}

.ap-inline-alert {
  font-size: 12px;
}

.ap-inline-alert :deep(.n-alert-body) {
  padding-top: 8px;
  padding-bottom: 8px;
}

.ap-inline-alert :deep(.n-alert__icon) {
  top: 50%;
  margin-top: 0;
  margin-bottom: 0;
  transform: translateY(-50%);
}

.ap-inline-alert :deep(.n-alert-body__content) {
  display: flex;
  align-items: center;
  width: 100%;
}

.ap-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent);
}

.dot-running {
  background: var(--color-success, #22c55e);
  color: var(--color-success, #22c55e);
  animation: ap-dot-pulse 1.4s ease-in-out infinite;
}

.dot-review {
  background: var(--color-warning, #f59e0b);
  color: var(--color-warning, #f59e0b);
  animation: ap-dot-pulse 0.9s ease-in-out infinite;
}

.dot-error {
  background: var(--color-danger, #ef4444);
  color: var(--color-danger, #ef4444);
}

.dot-stopped {
  background: var(--app-text-muted);
  color: var(--app-text-muted);
}

@keyframes ap-dot-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.55; transform: scale(0.92); }
}

.ap-stage-tag {
  font-size: 12px;
  padding: 4px 11px;
  border-radius: 999px;
  font-weight: 600;
  border: 1px solid transparent;
}

.tag-review {
  background: var(--color-warning-dim);
  color: var(--color-warning);
  border-color: color-mix(in srgb, var(--color-warning) 25%, transparent);
}

.tag-idle {
  background: color-mix(in srgb, var(--app-text-muted) 12%, transparent);
  color: var(--app-text-muted);
}

.tag-sem-plan { background: var(--color-brand-light); color: var(--color-brand); }
.tag-sem-write { background: var(--color-success-dim); color: var(--color-success); }
.tag-sem-audit { background: var(--color-warning-dim); color: var(--color-warning); }
.tag-sem-sync { background: var(--color-info-dim); color: var(--color-info); }
.tag-sem-review { background: var(--color-warning-dim); color: var(--color-warning); }
.tag-sem-idle {
  background: var(--color-purple-light, rgba(139, 92, 246, 0.12));
  color: var(--color-purple, #8b5cf6);
}
.tag-sem-daemon_wait { background: var(--color-info-dim); color: var(--color-info); }

.stage-text { vertical-align: middle; }

.ap-stage-live {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 6px;
  border-radius: 50%;
  background: currentColor;
  vertical-align: middle;
  animation: ap-live-pulse 1.2s ease-in-out infinite;
}

@keyframes ap-live-pulse {
  0%, 100% { opacity: 0.9; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.88); }
}

.tag-transitioning { position: relative; overflow: hidden; }

.skeleton-inline {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  z-index: 1;
}

.skeleton-pulse {
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--color-primary) 6%, transparent) 25%,
    color-mix(in srgb, var(--color-primary) 18%, transparent) 50%,
    color-mix(in srgb, var(--color-primary) 6%, transparent) 75%
  );
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s ease-in-out infinite;
}

@keyframes skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.stage-transition-label {
  position: relative;
  z-index: 2;
  animation: fade-in-up 0.35s ease;
}

@keyframes fade-in-up {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.ap-sse-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid var(--app-border);
}

.ap-sse-pill.is-on {
  background: var(--color-success-dim);
  color: var(--color-success);
  border-color: color-mix(in srgb, var(--color-success) 30%, transparent);
}

.ap-sse-pill.is-off {
  background: color-mix(in srgb, var(--app-text-muted) 10%, transparent);
  color: var(--app-text-muted);
}

.ap-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.ap-kpi {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 12px 10px;
  min-width: 0;
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md, 10px);
  transition: border-color var(--app-transition), box-shadow var(--app-transition);
}

.ap-kpi:hover {
  border-color: var(--app-border-strong);
  box-shadow: var(--app-shadow-sm);
}

.ap-kpi__label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--app-text-muted);
}

.ap-kpi__value {
  font-size: 15px;
  font-weight: 650;
  color: var(--app-text-primary);
  font-variant-numeric: tabular-nums;
  line-height: 1.25;
}

.ap-kpi__value--wrap {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.45;
}

.ap-kpi--location {
  gap: 7px;
  padding-bottom: 11px;
}

.ap-kpi__sep {
  margin: 0 2px;
  color: var(--app-text-muted);
  font-weight: 500;
}

.ap-kpi__act {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  font-weight: 500;
  color: var(--app-text-secondary);
}

.ap-kpi__muted {
  color: var(--app-text-muted);
  font-weight: 500;
}

.ap-location {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.ap-location__meta {
  width: fit-content;
  max-width: 100%;
  padding: 2px 7px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--color-primary, #2563eb) 18%, var(--app-border));
  background: color-mix(in srgb, var(--color-primary, #2563eb) 7%, transparent);
  color: var(--app-text-secondary);
  font-size: 10px;
  font-weight: 700;
  line-height: 1.35;
  font-variant-numeric: tabular-nums;
}

.ap-location__title {
  min-width: 0;
  max-width: 100%;
  color: var(--app-text-primary);
  font-size: 13px;
  font-weight: 700;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ap-location__trail {
  min-height: 20px;
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.ap-location__chip {
  min-width: 0;
  max-width: 100%;
  padding: 2px 6px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--app-text-muted) 8%, transparent);
  color: var(--app-text-muted);
  font-size: 10.5px;
  font-weight: 650;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ap-location__chip--strong {
  background: var(--color-primary-light, color-mix(in srgb, var(--color-primary, #2563eb) 12%, transparent));
  color: var(--color-primary, #2563eb);
}

.ap-narrative {
  padding: 12px 14px;
  border-radius: var(--app-radius-md, 10px);
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-left: 3px solid var(--color-primary, #2563eb);
}

.ap-narrative__label {
  display: block;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--app-text-muted);
  margin-bottom: 6px;
}

.ap-narrative__title {
  display: block;
  font-weight: 600;
  color: var(--app-text-primary);
  margin-bottom: 4px;
}

.ap-narrative__body {
  margin: 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--app-text-secondary);
}

.ap-narrative__body--muted {
  font-style: italic;
  color: var(--app-text-muted);
}

.ap-telemetry {
  padding: 12px 14px;
  border-radius: var(--app-radius-md, 10px);
  background: color-mix(in srgb, var(--color-primary) 4%, var(--app-surface-subtle));
  border: 1px solid color-mix(in srgb, var(--color-primary) 18%, var(--app-border));
}

.ap-telemetry__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.ap-telemetry__title {
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--app-text-muted);
}

.ap-telemetry__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 14px;
}

.ap-telemetry__item {
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto;
  gap: 4px 10px;
  align-items: center;
}

.ap-telemetry__item--wide {
  grid-column: 1 / -1;
}

.ap-telemetry__key {
  font-size: 11px;
  color: var(--app-text-muted);
  font-weight: 500;
}

.ap-telemetry__val {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-primary);
  font-variant-numeric: tabular-nums;
  justify-self: end;
}

.ap-telemetry__val--focus {
  justify-self: start;
  grid-column: 2;
  font-weight: 500;
  color: var(--app-text-secondary);
  word-break: break-word;
}

.ap-meter {
  grid-column: 1 / -1;
  height: 4px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--app-text-primary) 8%, transparent);
  overflow: hidden;
}

.ap-meter__fill {
  height: 100%;
  border-radius: inherit;
  transition: width 0.45s ease;
}

.ap-meter__fill--beat {
  background: linear-gradient(90deg, var(--color-primary), var(--color-brand-hover, #3b82f6));
}

.ap-meter__fill--word {
  background: linear-gradient(90deg, var(--color-success), color-mix(in srgb, var(--color-success) 70%, #fff));
}

.substep-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: var(--color-brand-light);
  color: var(--color-brand);
}

.substep-badge.substep-active {
  background: var(--color-success-dim);
  color: var(--color-success);
  animation: pulse-subtle 2s infinite;
}

.substep-badge.substep-prepare,
.substep-badge.substep-plan {
  background: var(--color-info-dim, var(--color-brand-light));
  color: var(--color-info, var(--color-brand));
}

.substep-badge.substep-finish {
  background: var(--color-warning-dim);
  color: var(--color-warning);
}

.substep-badge.substep-audit {
  background: var(--color-warning-dim);
  color: var(--color-warning);
}

@keyframes pulse-subtle {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.72; }
}

.pct-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--color-success-dim);
  color: var(--color-success);
  font-weight: 600;
  margin-left: 4px;
}

@media (max-width: 900px) {
  .ap-kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .ap-telemetry__grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 520px) {
  .ap-hero__pct-value {
    font-size: 22px;
  }
}

.ap-review-alert {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

.ap-review-alert span {
  line-height: 1.55;
  min-width: 0;
}

.ap-review-alert .n-button {
  flex: 0 0 auto;
}

.recovery-hint p { margin: 0 0 6px; line-height: 1.5; }
.recovery-sub { font-size: 11px; opacity: 0.95; margin-bottom: 8px !important; }

@media (max-width: 640px) {
  .ap-review-alert {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
