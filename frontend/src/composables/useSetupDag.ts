import { computed, onScopeDispose, ref } from 'vue'
import { setupDagApi, type SetupDagRun, type SetupDagStage, type SetupStageId } from '@/api/setupDag'
import { formatApiError } from '@/utils/apiError'

interface SetupDagCallbacks {
  onUpdate: (run: SetupDagRun, isCurrent: () => boolean) => void
  onCompleted: (run: SetupDagRun, isCurrent: () => boolean) => Promise<boolean>
  onFailure: (stage: SetupStageId, error: string) => void
}

export function useSetupDag(novelId: () => string, callbacks: SetupDagCallbacks) {
  const stages = ref<SetupDagStage[]>([])
  const run = ref<SetupDagRun | null>(null)
  const starting = ref(false)
  const stopping = ref(false)
  const loading = ref(false)
  const error = ref('')
  const connectionLost = ref(false)
  const busy = computed(() => starting.value || run.value?.status === 'running')
  let timer: ReturnType<typeof setTimeout> | null = null
  let revision = 0
  let observing = false
  let disposed = false
  let failures = 0

  function pause() {
    observing = false
    revision += 1
    if (timer != null) clearTimeout(timer)
    timer = null
    loading.value = false
    starting.value = false
    stopping.value = false
  }
  const current = (request: number) => observing && !disposed && request === revision
  function schedule(request: number) {
    if (!current(request) || run.value?.status !== 'running') return
    if (timer != null) clearTimeout(timer)
    timer = setTimeout(() => { timer = null; void poll(request) }, 1200)
  }
  async function accept(snapshot: SetupDagRun, request: number) {
    if (!current(request)) return
    run.value = snapshot
    stages.value = stages.value.map(stage => stage.id === snapshot.stage ? { ...stage, latest_run: snapshot } : stage)
    // Historical setup failures predate audit checkpoints and must not lock existing books.
    if ((snapshot.workflow_version ?? 1) < 2 && !['running', 'completed'].includes(snapshot.status)) return
    callbacks.onUpdate(snapshot, () => current(request) && run.value?.run_id === snapshot.run_id)
    if (snapshot.status === 'running') {
      schedule(request)
    } else if (snapshot.status === 'completed') {
      const loaded = await callbacks.onCompleted(snapshot, () => current(request))
      if (!current(request)) return
      if (!loaded) error.value = '生成结果已保存，但读取失败，请检查连接后重新连接。'
    } else {
      const detail = snapshot.error || (snapshot.status === 'cancelled' ? '生成已停止，已完成的步骤已保留，可继续生成。' : '生成任务已中断，已完成的步骤已保留，可继续生成。')
      error.value = detail
      callbacks.onFailure(snapshot.stage, detail)
    }
  }
  async function poll(request: number) {
    const snapshot = run.value
    if (!snapshot || !current(request)) return
    try {
      const next = await setupDagApi.getRun(novelId(), snapshot.run_id)
      if (!current(request)) return
      failures = 0
      error.value = ''
      await accept(next, request)
    } catch (reason) {
      if (!current(request)) return
      failures += 1
      if (failures < 3) schedule(request)
      else {
        connectionLost.value = true
        error.value = `无法获取生成进度：${formatApiError(reason, '连接失败')}。后台任务可能仍在运行，请重新连接。`
      }
    }
  }
  async function resume() {
    pause()
    observing = true
    const request = revision
    loading.value = true
    error.value = ''
    connectionLost.value = false
    failures = 0
    try {
      const data = await setupDagApi.get(novelId())
      if (!current(request)) return
      stages.value = data.stages
      const latest = data.stages.map(stage => stage.latest_run).filter((item): item is SetupDagRun => Boolean(item))
        .sort((a, b) => b.started_at.localeCompare(a.started_at))[0]
      const snapshot = data.active_run || latest || null
      run.value = snapshot
      if (snapshot) await accept(snapshot, request)
    } catch (reason) {
      if (!current(request)) return
      connectionLost.value = true
      error.value = `无法读取设置工作流：${formatApiError(reason, '连接失败')}`
    } finally {
      if (current(request)) loading.value = false
    }
  }
  async function start(stage: SetupStageId, restart = false) {
    if (busy.value || loading.value || disposed) return
    pause()
    observing = true
    const request = revision
    starting.value = true
    error.value = ''
    connectionLost.value = false
    failures = 0
    try {
      const snapshot = await setupDagApi.start(novelId(), stage, restart)
      await accept(snapshot, request)
    } catch (reason) {
      if (!current(request)) return
      const detail = formatApiError(reason, '启动生成失败')
      error.value = detail
      callbacks.onFailure(stage, detail)
    } finally {
      if (current(request)) starting.value = false
    }
  }
  async function stop() {
    if (!run.value || stopping.value || disposed) return
    // Revoke in-flight progress requests before sending the stop command.
    const request = ++revision
    if (timer != null) clearTimeout(timer)
    timer = null
    stopping.value = true
    try {
      await accept(await setupDagApi.stop(novelId(), run.value.run_id), request)
    } catch (reason) {
      if (current(request)) {
        error.value = `停止失败：${formatApiError(reason)}`
        schedule(request)
      }
    } finally {
      if (current(request)) stopping.value = false
    }
  }
  onScopeDispose(() => { pause(); disposed = true })
  return { stages, run, starting, stopping, loading, error, connectionLost, busy, start, resume, pause, stop }
}
