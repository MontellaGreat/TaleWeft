/**
 * DAG 运行状态管理 — 运行控制、历史记录、SSE 事件连接
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { DAGRunResult, DAGStatusResponse, NodeEvent, NodeStatus } from '@/types/dag'
import { dagApi, type DAGExecutionCapabilities } from '@/api/dag'
import { autopilotApi } from '@/api/autopilot'
import { createAuthenticatedEventSource } from '@/api/sessionEvents'
import { runtimePerformance } from '@/config/performance'

export type DAGRunStatus = 'idle' | 'running' | 'stopping' | 'completed' | 'error' | 'cancelled' | 'awaiting_review'

export const useDAGRunStore = defineStore('dagRun', () => {
  // ─── 运行状态 ───
  const runStatus = ref<DAGRunStatus>('idle')
  const currentRunId = ref<string | null>(null)
  const dagEnabled = ref(false)
  const currentVersion = ref(0)
  const executionMode = ref<'autopilot_projection' | 'dag_runner'>('autopilot_projection')
  const chapterNumber = ref<number | null>(null)
  const capabilities = ref<DAGExecutionCapabilities>({ can_run: false, can_stop: false, can_configure: false, can_checkpoint: false, can_rewrite: false, reason: 'DAG 独立运行暂不可用，请使用全托管控制。' })
  const capabilityReason = computed(() => capabilities.value.reason)

  // ─── 节点运行时状态快照 ───
  const nodeStates = ref<DAGStatusResponse['node_states']>({})

  // ─── 运行历史 ───
  const runHistory = ref<DAGRunResult[]>([])
  const latestResult = ref<DAGRunResult | null>(null)

  // ─── SSE 连接 ───
  const sseConnected = ref(false)
  const sseError = ref<string | null>(null)
  let _eventSource: EventSource | null = null
  let _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let _reconnectAttempts = 0

  // ─── 计算属性 ───
  const isRunning = computed(() => runStatus.value === 'running')
  const canStart = computed(() => capabilities.value.can_run && (runStatus.value === 'idle' || runStatus.value === 'completed' || runStatus.value === 'error' || runStatus.value === 'cancelled' || runStatus.value === 'awaiting_review'))
  const canStop = computed(() => capabilities.value.can_stop && runStatus.value === 'running')

  // ─── 运行控制 ───

  async function startRun(novelId: string, request: { chapter_number?: number; resume_run_id?: string } = {}) {
    if (!canStart.value) return
    try {
      const result = await dagApi.runDAG(novelId, request)
      if (result.status !== 'started' || !result.dag_run_id) throw new Error('DAG 启动未获得运行确认')
      currentRunId.value = result.dag_run_id
      chapterNumber.value = result.chapter_number
      executionMode.value = 'dag_runner'
      nodeStates.value = {}
      latestResult.value = null
      runStatus.value = 'running'
    } catch (e: unknown) {
      runStatus.value = 'error'
      sseError.value = e instanceof Error ? e.message : '启动运行失败'
      throw e
    }
  }

  async function stopRun(novelId: string) {
    if (!canStop.value) return
    try {
      runStatus.value = 'stopping'
      const result = await dagApi.stopDAG(novelId, currentRunId.value)
      if (!result.quiescent || result.status !== 'stopped') throw new Error('DAG 停止尚未确认')
      await fetchStatus(novelId)
    } catch (e: unknown) {
      sseError.value = e instanceof Error ? e.message : '停止运行失败'
      runStatus.value = 'running'
      throw e
    }
  }

  async function fetchStatus(novelId: string) {
    try {
      const status = await dagApi.getStatus(novelId)
      dagEnabled.value = status.dag_enabled
      currentVersion.value = status.current_version
      capabilities.value = status.capabilities || capabilities.value
      nodeStates.value = status.node_states

      executionMode.value = status.execution_mode
      if (status.execution_mode === 'dag_runner') {
        currentRunId.value = status.dag_run_id || null
        chapterNumber.value = status.chapter_number || null
        const authoritative = status.status || 'idle'
        runStatus.value = (['running', 'stopping', 'completed', 'error', 'cancelled', 'awaiting_review'].includes(authoritative) ? authoritative : authoritative === 'interrupted' ? 'error' : 'idle') as DAGRunStatus
        if (status.result) latestResult.value = status.result
        if (status.error) sseError.value = status.error
      }
      for (const [nodeId, state] of Object.entries(status.node_states)) {
        handleNodeStatusChange({ ...state, type: 'node_status_change', novel_id: novelId, node_id: nodeId,
          dag_run_id: status.dag_run_id || undefined, timestamp: new Date().toISOString(), source: status.execution_mode })
      }
    } catch {
      // 静默失败
    }
  }

  // ─── SSE 事件连接 ───

  function connectSSE(novelId: string, options: { resetReconnect?: boolean } = {}) {
    disconnectSSE({ resetReconnect: options.resetReconnect ?? true })

    // 构建 SSE URL（由 dagApi 兼容 Tauri 桌面模式）
    const url = dagApi.eventsUrl(novelId)

    try {
      const source = createAuthenticatedEventSource(url)
      _eventSource = source
      sseConnected.value = false
      sseError.value = null

      source.onopen = () => {
        if (_eventSource !== source) return
        sseConnected.value = true
        sseError.value = null
        _reconnectAttempts = 0
      }

      source.onmessage = (event) => {
        if (_eventSource !== source) return
        try {
          const data = JSON.parse(event.data) as NodeEvent
          handleSSEMessage(data)
        } catch {
          // 忽略解析错误
        }
      }

      // 监听特定事件类型
      source.addEventListener('node_status_change', (event) => {
        if (_eventSource !== source) return
        try {
          const data = JSON.parse((event as MessageEvent).data) as NodeEvent
          handleNodeStatusChange(data)
        } catch { /* ignore */ }
      })

      source.addEventListener('node_output', (event) => {
        if (_eventSource !== source) return
        try {
          const data = JSON.parse((event as MessageEvent).data) as NodeEvent
          handleNodeOutput(data)
        } catch { /* ignore */ }
      })

      source.addEventListener('edge_data_flow', (event) => {
        if (_eventSource !== source) return
        try {
          const data = JSON.parse((event as MessageEvent).data) as NodeEvent
          handleEdgeFlow(data)
        } catch { /* ignore */ }
      })

      source.addEventListener('dag_run_state', () => {
        if (_eventSource === source) void fetchStatus(novelId)
      })

      source.addEventListener('dag_run_complete', (event) => {
        if (_eventSource !== source) return
        try {
          const data = JSON.parse((event as MessageEvent).data) as DAGRunResult
          handleDAGRunComplete(data)
        } catch { /* ignore */ }
      })

      source.onerror = () => {
        if (_eventSource !== source) return
        source.close()
        _eventSource = null
        sseConnected.value = false
        // 自动重连
        scheduleReconnect(novelId)
      }
    } catch (e: unknown) {
      sseError.value = e instanceof Error ? e.message : 'SSE 连接失败'
      scheduleReconnect(novelId)
    }
  }

  function disconnectSSE(options: { resetReconnect?: boolean } = {}) {
    if (_eventSource) {
      _eventSource.close()
      _eventSource = null
    }
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer)
      _reconnectTimer = null
    }
    if (options.resetReconnect ?? true) {
      _reconnectAttempts = 0
    }
    sseConnected.value = false
  }

  function scheduleReconnect(novelId: string) {
    if (_reconnectTimer) return
    _reconnectAttempts += 1
    const perf = runtimePerformance.dagSse
    const delayMs = Math.min(
      perf.reconnectBaseDelayMs * (2 ** (_reconnectAttempts - 1)),
      perf.reconnectMaxDelayMs,
    )
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null
      connectSSE(novelId, { resetReconnect: false })
    }, delayMs)
  }

  // ─── SSE 事件处理回调 ───
  // 通过注册回调与 dagStore 解耦

  type SSECallback = (event: NodeEvent) => void
  type RunCompleteCallback = (result: DAGRunResult) => void

  const _nodeStatusCallbacks: SSECallback[] = []
  const _nodeOutputCallbacks: SSECallback[] = []
  const _edgeFlowCallbacks: SSECallback[] = []
  const _runCompleteCallbacks: RunCompleteCallback[] = []

  function removeCallback<T>(callbacks: T[], cb: T) {
    const ix = callbacks.indexOf(cb)
    if (ix >= 0) callbacks.splice(ix, 1)
  }

  function onNodeStatusChange(cb: SSECallback) {
    _nodeStatusCallbacks.push(cb)
    return () => removeCallback(_nodeStatusCallbacks, cb)
  }
  function onNodeOutput(cb: SSECallback) {
    _nodeOutputCallbacks.push(cb)
    return () => removeCallback(_nodeOutputCallbacks, cb)
  }
  function onEdgeFlow(cb: SSECallback) {
    _edgeFlowCallbacks.push(cb)
    return () => removeCallback(_edgeFlowCallbacks, cb)
  }
  function onRunComplete(cb: RunCompleteCallback) {
    _runCompleteCallbacks.push(cb)
    return () => removeCallback(_runCompleteCallbacks, cb)
  }

  function handleSSEMessage(event: NodeEvent) {
    // 通用消息分发
    switch (event.type) {
      case 'node_status_change':
        handleNodeStatusChange(event)
        break
      case 'node_output':
        handleNodeOutput(event)
        break
      case 'edge_data_flow':
        handleEdgeFlow(event)
        break
    }
  }

  function handleNodeStatusChange(event: NodeEvent) {
    if (event.source === 'dag_runner') {
      executionMode.value = 'dag_runner'
      if (event.dag_run_id && currentRunId.value && event.dag_run_id !== currentRunId.value) return
      if (event.dag_run_id) currentRunId.value = event.dag_run_id
    } else if (executionMode.value === 'dag_runner') return
    // 更新本地节点状态
    if (event.node_id && event.status) {
      const existing = nodeStates.value[event.node_id] || { status: 'idle' as NodeStatus, enabled: true }
      // SSE metrics are intentionally untrusted JSON.  Keep the status
      // snapshot's numeric contract so widgets can safely call toFixed()/
      // arithmetic without receiving strings or nested values from a
      // provider-specific event payload.
      const numericMetrics = event.metrics
        ? Object.fromEntries(
            Object.entries(event.metrics)
              .map(([key, value]) => [key, Number(value)] as const)
              .filter(([, value]) => Number.isFinite(value)),
          )
        : {}
      nodeStates.value[event.node_id] = {
        ...existing,
        status: event.status,
        metrics: { ...(existing.metrics ?? {}), ...numericMetrics },
        outputs: event.outputs ?? existing.outputs,
        duration_ms: event.duration_ms ?? existing.duration_ms,
        error: event.error ?? existing.error,
        ...(event.attempt !== undefined ? { attempt: event.attempt } : {}),
      }

      // Only an explicit dag_run_complete event proves completion. Projected
      // node highlights and local counts cannot prove an executed DAG succeeded.
    }
    // 通知回调
    _nodeStatusCallbacks.forEach(cb => cb(event))
  }

  function handleNodeOutput(event: NodeEvent) {
    _nodeOutputCallbacks.forEach(cb => cb(event))
  }

  function handleEdgeFlow(event: NodeEvent) {
    _edgeFlowCallbacks.forEach(cb => cb(event))
  }

  function handleDAGRunComplete(result: DAGRunResult) {
    if (currentRunId.value && result.dag_run_id !== currentRunId.value) return
    currentRunId.value = result.dag_run_id
    executionMode.value = 'dag_runner'
    runStatus.value = result.status === 'interrupted' ? 'error' : result.status
    latestResult.value = result
    runHistory.value.unshift(result)
    // 只保留最近 20 条
    if (runHistory.value.length > 20) {
      runHistory.value = runHistory.value.slice(0, 20)
    }
    _runCompleteCallbacks.forEach(cb => cb(result))
  }

  // ─── 托管模式日志流连接（桥接到 DAG 节点状态） ───

  let _autopilotLogSource: EventSource | null = null
  let _autopilotLogCallback: ((data: { type: string; message: string; metadata?: Record<string, unknown> }) => void) | null = null

  function connectAutopilotLog(
    novelId: string,
    callback: (data: { type: string; message: string; metadata?: Record<string, unknown> }) => void,
  ) {
    disconnectAutopilotLog()
    _autopilotLogCallback = callback

    const url = autopilotApi.logStreamUrl(novelId)
    try {
      _autopilotLogSource = createAuthenticatedEventSource(url)

      _autopilotLogSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (_autopilotLogCallback && data.type !== 'heartbeat' && data.type !== 'connected') {
            _autopilotLogCallback({
              type: data.type || 'log',
              message: data.message || '',
              metadata: data.metadata || data.meta || {},
            })
          }
        } catch {
          // 忽略解析错误
        }
      }

      _autopilotLogSource.onerror = () => {
        // 静默失败，不重连（DAG SSE 已有自己的重连机制）
      }
    } catch {
      // 连接失败，静默
    }
  }

  function disconnectAutopilotLog() {
    if (_autopilotLogSource) {
      _autopilotLogSource.close()
      _autopilotLogSource = null
    }
    _autopilotLogCallback = null
  }

  // ─── 重置 ───

  function resetForNovel(novelId: string) {
    runStatus.value = 'idle'
    currentRunId.value = null
    executionMode.value = 'autopilot_projection'
    chapterNumber.value = null
    latestResult.value = null
    nodeStates.value = {}
    capabilities.value = { can_run: false, can_stop: false, can_configure: false, can_checkpoint: false, can_rewrite: false, reason: 'DAG 独立运行暂不可用，请使用全托管控制。' }
    sseError.value = null
    disconnectSSE()
    disconnectAutopilotLog()
  }

  return {
    // State
    runStatus,
    currentRunId,
    dagEnabled,
    currentVersion,
    executionMode,
    chapterNumber,
    capabilities,
    capabilityReason,
    nodeStates,
    runHistory,
    latestResult,
    sseConnected,
    sseError,

    // Computed
    isRunning,
    canStart,
    canStop,

    // Actions
    startRun,
    stopRun,
    fetchStatus,
    connectSSE,
    disconnectSSE,
    resetForNovel,

    // Event callbacks
    onNodeStatusChange,
    onNodeOutput,
    onEdgeFlow,
    onRunComplete,

    // Autopilot log bridge
    connectAutopilotLog,
    disconnectAutopilotLog,
  }
})
