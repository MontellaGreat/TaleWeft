import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { featureFlags } from '../config/features'
import { runtimePerformance } from '../config/performance'
import { invocationFailureMessage } from '../utils/invocationFailure'
import {
  aiInvocationApi,
  type InvocationPromptDraftPreviewDTO,
  type AdoptionCommitDTO,
  type AdoptionDecisionDTO,
  type InvocationAttemptDTO,
  type InvocationResponseDTO,
  type InvocationSessionDTO,
  type InvocationVariableSnapshotGroup,
} from '../api/aiInvocation'

function errorText(err: unknown): string {
  if (err instanceof Error && err.message.trim()) return err.message
  if (typeof err === 'string' && err.trim()) return err
  return '操作失败，请稍后重试'
}

export const useAIInvocationStore = defineStore('aiInvocation', () => {
  type SessionListener = { update: (payload: InvocationResponseDTO) => void; error?: (message: string) => void }
  const sessionListeners = new Map<string, SessionListener[]>()
  const sessionPollFailures = new Map<string, number>()
  const sessionPollTimer = new Map<string, number>()
  const activeGenerationPollSessions = new Set<string>()
  const sessionPollInFlight = new Set<string>()
  const headlessAdvancingSessions = new Set<string>()
  const visible = ref(false)
  const loading = ref(false)
  const actionLoading = ref(false)
  const error = ref('')
  const session = ref<InvocationSessionDTO | null>(null)
  const attempt = ref<InvocationAttemptDTO | null>(null)
  const decision = ref<AdoptionDecisionDTO | null>(null)
  const commit = ref<AdoptionCommitDTO | null>(null)
  const nextAction = ref('')
  const promptDraftSystem = ref('')
  const promptDraftUser = ref('')
  const promptDraftSavedSystem = ref('')
  const promptDraftSavedUser = ref('')
  const promptDraftPreview = ref<InvocationPromptDraftPreviewDTO | null>(null)
  const promptDraftLoading = ref(false)
  const liveAttemptContent = ref('')
  const liveAttemptLoading = ref(false)

  const hasAttempt = computed(() => Boolean(attempt.value?.id))
  const canAccept = computed(() => Boolean(
    session.value?.id
    && session.value.status === 'awaiting_acceptance'
    && attempt.value?.id
    && attempt.value.status === 'succeeded'
    && !decision.value?.id,
  ))
  const canCommit = computed(() => Boolean(session.value?.id && decision.value?.id && !commit.value?.id))
  const canRetry = computed(() => Boolean(
    session.value?.id
    && attempt.value?.id
    && ['awaiting_pre_call_review', 'awaiting_acceptance', 'awaiting_commit', 'cancelled', 'failed'].includes(String(session.value.status || '')),
  ))
  const isGenerating = computed(() => session.value?.status === 'generating')
  const liveAttemptDisplay = computed(() => liveAttemptContent.value || attempt.value?.content || '')
  const title = computed(() => {
    if (!session.value) return 'AI 生成审阅'
    return `${session.value.operation} / ${session.value.node_key}`
  })
  const draftSystemTemplate = computed(
    () => session.value?.prompt_snapshot?.template_prompt?.system || '',
  )
  const draftSystemEdited = computed(
    () => promptDraftSystem.value || promptDraftSavedSystem.value || draftSystemTemplate.value,
  )
  const draftUserTemplate = computed(
    () => session.value?.prompt_snapshot?.template_prompt?.user || '',
  )
  const draftUserEdited = computed(
    () => promptDraftUser.value || promptDraftSavedUser.value || draftUserTemplate.value,
  )
  const draftRuntimeSystem = computed(
    () => promptDraftPreview.value?.prompt_snapshot?.prompt?.system
      || session.value?.prompt_snapshot?.prompt?.system
      || '',
  )
  const draftRuntimeUser = computed(
    () => promptDraftPreview.value?.prompt_snapshot?.prompt?.user
      || session.value?.prompt_snapshot?.prompt?.user
      || '',
  )
  const draftDiagnostics = computed(
    () => promptDraftPreview.value?.prompt_snapshot?.diagnostics
      || session.value?.prompt_snapshot?.diagnostics
      || [],
  )
  const draftMissingVariables = computed(
    () => promptDraftPreview.value?.prompt_snapshot?.missing_variables
      || session.value?.prompt_snapshot?.missing_variables
      || [],
  )
  const variableSnapshotGroups = computed(() => {
    const plan = promptDraftPreview.value?.variable_plan || session.value?.variable_plan
    return plan?.snapshot_groups ?? []
  })
  const debugPanelEnabled = computed(() => featureFlags.aiInvocationDebug)

  function showDebugPanel() {
    if (debugPanelEnabled.value) {
      visible.value = true
    }
  }

  function shouldKeepPanelVisible(): boolean {
    return visible.value || debugPanelEnabled.value
  }

  function scheduleHeadlessAdvance() {
    if (debugPanelEnabled.value) return
    const sessionId = session.value?.id
    if (!sessionId || headlessAdvancingSessions.has(sessionId)) return
    void advanceHeadlessSession(sessionId)
  }

  function shouldCommitPromptVersion(): boolean {
    const snapshot = session.value?.prompt_snapshot
    const draft = snapshot?.draft_prompt
    const template = snapshot?.template_prompt
    if (!draft) return false
    if (!template) return true
    return draft.system !== template.system || draft.user !== template.user
  }

  function applyResponse(payload: InvocationResponseDTO) {
    const previousSessionId = session.value?.id ?? null
    const nextSessionId = payload.session?.id ?? null
    const sameSession = previousSessionId !== null && previousSessionId === nextSessionId

    session.value = payload.session
    attempt.value = payload.attempt ?? (sameSession ? attempt.value ?? null : null)
    decision.value = payload.decision ?? (sameSession ? decision.value ?? null : null)
    commit.value = payload.commit ?? (sameSession ? commit.value ?? null : null)
    nextAction.value = payload.next_action ?? ''
    promptDraftSavedSystem.value = payload.session?.prompt_snapshot?.draft_prompt?.system
      ?? payload.session?.prompt_snapshot?.template_prompt?.system
      ?? ''
    promptDraftSavedUser.value = payload.session?.prompt_snapshot?.draft_prompt?.user
      ?? payload.session?.prompt_snapshot?.template_prompt?.user
      ?? ''
    promptDraftSystem.value = promptDraftSavedSystem.value
    promptDraftUser.value = promptDraftSavedUser.value
    promptDraftPreview.value = null
    if (payload.attempt?.content != null) {
      liveAttemptContent.value = payload.attempt.content
    } else if (!sameSession) {
      liveAttemptContent.value = ''
    }
    syncGenerationPolling()
    const listeners = payload.session?.id ? sessionListeners.get(payload.session.id) : undefined
    if (listeners?.length) {
      for (const listener of [...listeners]) {
        listener.update(payload)
      }
    }
    scheduleHeadlessAdvance()
  }

  async function advanceHeadlessSession(sessionId: string) {
    headlessAdvancingSessions.add(sessionId)
    try {
      for (let step = 0; step < 4; step += 1) {
        if (debugPanelEnabled.value || session.value?.id !== sessionId) return
        if (invocationFailureMessage({ session: session.value, attempt: attempt.value, commit: commit.value })) return
        const previousStatus = session.value.status
        if (session.value.status === 'awaiting_pre_call_review') {
          await resume()
        } else if (session.value.status === 'awaiting_acceptance' && attempt.value?.id) {
          await accept()
        } else if (session.value.status === 'awaiting_commit' && decision.value?.id) {
          await runCommit()
        } else if (['generating', 'committing', 'completed', 'failed', 'blocked', 'cancelled'].includes(session.value.status)) {
          return
        } else {
          throw new Error('生成会话缺少下一步所需的结果，请重试')
        }
        if (session.value?.id === sessionId && session.value.status === previousStatus) {
          throw new Error('自动生成未能进入下一阶段，请重试')
        }
      }
    } catch (err) {
      notifySessionError(sessionId, errorText(err))
    } finally {
      headlessAdvancingSessions.delete(sessionId)
    }
  }

  function notifySessionError(sessionId: string, message: string) {
    if (session.value?.id !== sessionId) return
    error.value = message
    stopGenerationPolling(sessionId)
    for (const listener of [...(sessionListeners.get(sessionId) ?? [])]) {
      listener.error?.(message)
    }
  }

  function openFromResponse(payload: InvocationResponseDTO, options: { showPanel?: boolean } = {}) {
    if (payload.session?.id && payload.session.id !== session.value?.id) {
      attempt.value = null
      decision.value = null
      commit.value = null
      nextAction.value = ''
      liveAttemptContent.value = ''
      promptDraftPreview.value = null
    }
    applyResponse(payload)
    if (options.showPanel !== false) {
      showDebugPanel()
    }
  }

  function clearPromptDraftPreview() {
    promptDraftPreview.value = null
  }

  async function open(sessionId: string, options: { showPanel?: boolean } = {}) {
    const showPanel = options.showPanel !== false
    if (showPanel) {
      showDebugPanel()
    }
    loading.value = true
    error.value = ''
    session.value = null
    attempt.value = null
    decision.value = null
    commit.value = null
    nextAction.value = ''
    promptDraftSystem.value = ''
    promptDraftUser.value = ''
    promptDraftSavedSystem.value = ''
    promptDraftSavedUser.value = ''
    promptDraftPreview.value = null
    liveAttemptContent.value = ''
    stopGenerationPolling()
    try {
      const payload = await aiInvocationApi.get(sessionId)
      promptDraftSavedSystem.value = payload.session?.prompt_snapshot?.draft_prompt?.system
        ?? payload.session?.prompt_snapshot?.template_prompt?.system
        ?? ''
      promptDraftSavedUser.value = payload.session?.prompt_snapshot?.draft_prompt?.user
        ?? payload.session?.prompt_snapshot?.template_prompt?.user
        ?? ''
      promptDraftSystem.value = promptDraftSavedSystem.value
      promptDraftUser.value = promptDraftSavedUser.value
      openFromResponse(payload, { showPanel })
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      loading.value = false
    }
  }

  async function accept() {
    if (!session.value?.id || !attempt.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.accept(session.value.id, {
        attempt_id: attempt.value.id,
        accepted_by: 'user',
        commit_prompt_version: shouldCommitPromptVersion(),
      })
      applyResponse(payload)
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  async function reject() {
    if (!session.value?.id || !attempt.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.reject(session.value.id, {
        attempt_id: attempt.value.id,
        accepted_by: 'user',
      })
      applyResponse(payload)
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  async function retry() {
    if (!session.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.retry(session.value.id, {
        resumed_by: 'user',
      })
      decision.value = null
      commit.value = null
      applyResponse(payload)
      if (shouldKeepPanelVisible()) {
        showDebugPanel()
      }
      syncGenerationPolling()
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  async function resume() {
    if (!session.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.resume(session.value.id, {
        resumed_by: 'user',
      })
      applyResponse(payload)
      if (shouldKeepPanelVisible()) {
        showDebugPanel()
      }
      syncGenerationPolling()
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  async function previewPromptDraft(systemTemplate: string, userTemplate?: string | null) {
    if (!session.value?.id) return
    promptDraftLoading.value = true
    try {
      const payload = await aiInvocationApi.previewPromptDraft(session.value.id, {
        system_template: systemTemplate,
        user_template: userTemplate,
      })
      promptDraftPreview.value = payload
    } finally {
      promptDraftLoading.value = false
    }
  }

  async function savePromptDraft(systemTemplate: string, userTemplate?: string | null) {
    if (!session.value?.id) return
    promptDraftLoading.value = true
    try {
      const payload = await aiInvocationApi.savePromptDraft(session.value.id, {
        system_template: systemTemplate,
        user_template: userTemplate,
      })
      promptDraftSavedSystem.value = systemTemplate
      promptDraftSavedUser.value = userTemplate ?? ''
      promptDraftPreview.value = null
      applyResponse(payload)
    } finally {
      promptDraftLoading.value = false
    }
  }

  async function updateVariables(values: Record<string, unknown>) {
    if (!session.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.updateVariables(session.value.id, {
        values,
        updated_by: 'user',
      })
      applyResponse(payload)
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  async function runCommit() {
    if (!session.value?.id || !decision.value?.id) return
    actionLoading.value = true
    error.value = ''
    try {
      const payload = await aiInvocationApi.commit(session.value.id, decision.value.id)
      applyResponse(payload)
    } catch (err) {
      error.value = errorText(err)
      throw err
    } finally {
      actionLoading.value = false
    }
  }

  function close() {
    visible.value = false
  }

  function clearGenerationPollTimer(sessionId: string) {
    const timer = sessionPollTimer.get(sessionId)
    if (!timer) return
    clearTimeout(timer)
    sessionPollTimer.delete(sessionId)
  }

  function refreshLiveAttemptLoading() {
    liveAttemptLoading.value = sessionPollTimer.size > 0 || sessionPollInFlight.size > 0
  }

  function stopGenerationPolling(sessionId?: string) {
    if (sessionId) {
      sessionPollFailures.delete(sessionId)
      activeGenerationPollSessions.delete(sessionId)
      clearGenerationPollTimer(sessionId)
    } else {
      sessionPollFailures.clear()
      activeGenerationPollSessions.clear()
      for (const activeSessionId of [...sessionPollTimer.keys()]) {
        clearGenerationPollTimer(activeSessionId)
      }
    }
    refreshLiveAttemptLoading()
  }

  async function refreshSession(sessionId = session.value?.id) {
    if (!sessionId) return
    const payload = await aiInvocationApi.get(sessionId, { silentGlobalFeedback: true })
    if (session.value?.id && session.value.id !== sessionId) return
    sessionPollFailures.delete(sessionId)
    applyResponse(payload)
  }

  function scheduleGenerationPoll(sessionId: string) {
    if (!activeGenerationPollSessions.has(sessionId)) return
    if (sessionPollTimer.has(sessionId) || sessionPollInFlight.has(sessionId)) return
    const timer = window.setTimeout(() => {
      sessionPollTimer.delete(sessionId)
      if (!activeGenerationPollSessions.has(sessionId)) {
        refreshLiveAttemptLoading()
        return
      }
      sessionPollInFlight.add(sessionId)
      refreshLiveAttemptLoading()
      void refreshSession(sessionId)
        .catch((err) => {
          if (!activeGenerationPollSessions.has(sessionId) || session.value?.id !== sessionId) return
          const failures = (sessionPollFailures.get(sessionId) ?? 0) + 1
          sessionPollFailures.set(sessionId, failures)
          if (failures >= 3) notifySessionError(sessionId, `无法获取生成进度：${errorText(err)}。请检查连接后重试。`)
        })
        .finally(() => {
          sessionPollInFlight.delete(sessionId)
          if (
            activeGenerationPollSessions.has(sessionId)
            && session.value?.id === sessionId
            && ['generating', 'committing'].includes(session.value?.status ?? '')
          ) {
            scheduleGenerationPoll(sessionId)
          }
          refreshLiveAttemptLoading()
        })
    }, runtimePerformance.aiInvocation.generationPollMs)
    sessionPollTimer.set(sessionId, timer)
    refreshLiveAttemptLoading()
  }

  function syncGenerationPolling() {
    const sessionId = session.value?.id
    if (!sessionId) return
    if (['generating', 'committing'].includes(session.value?.status ?? '')) {
      activeGenerationPollSessions.add(sessionId)
      scheduleGenerationPoll(sessionId)
      return
    }
    stopGenerationPolling()
  }

  function onSessionUpdate(sessionId: string, update: (payload: InvocationResponseDTO) => void, onError?: (message: string) => void) {
    const listener: SessionListener = { update, error: onError }
    const listeners = sessionListeners.get(sessionId) ?? []
    listeners.push(listener)
    sessionListeners.set(sessionId, listeners)
    return () => {
      const current = sessionListeners.get(sessionId)
      if (!current) return
      const remaining = current.filter(item => item !== listener)
      if (remaining.length) sessionListeners.set(sessionId, remaining)
      else sessionListeners.delete(sessionId)
    }
  }

  return {
    visible,
    loading,
    actionLoading,
    error,
    session,
    attempt,
    decision,
    commit,
    nextAction,
    promptDraftSystem,
    promptDraftUser,
    promptDraftSavedSystem,
    promptDraftSavedUser,
    promptDraftPreview,
    promptDraftLoading,
    liveAttemptContent,
    liveAttemptDisplay,
    liveAttemptLoading,
    draftSystemTemplate,
    draftSystemEdited,
    draftUserTemplate,
    draftUserEdited,
    draftRuntimeSystem,
    draftRuntimeUser,
    draftDiagnostics,
    draftMissingVariables,
    variableSnapshotGroups,
    debugPanelEnabled,
    hasAttempt,
    canAccept,
    canCommit,
    canRetry,
    isGenerating,
    title,
    open,
    openFromResponse,
    clearPromptDraftPreview,
    accept,
    reject,
    retry,
    resume,
    previewPromptDraft,
    savePromptDraft,
    updateVariables,
    runCommit,
    close,
    stopGenerationPolling,
    onSessionUpdate,
  }
})
