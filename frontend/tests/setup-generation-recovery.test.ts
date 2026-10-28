import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { useAIInvocationStore } from '../src/stores/aiInvocationStore'
import type { InvocationResponseDTO } from '../src/api/aiInvocation'

const state = vi.hoisted(() => ({
  features: { aiInvocationDebug: false },
  api: { get: vi.fn(), resume: vi.fn(), accept: vi.fn(), commit: vi.fn(), retry: vi.fn() },
}))
vi.mock('../src/config/features', () => ({ featureFlags: state.features }))
vi.mock('../src/api/aiInvocation', () => ({ aiInvocationApi: state.api }))
const stores: ReturnType<typeof useAIInvocationStore>[] = []
function payload(status: string, extra: Partial<InvocationResponseDTO> = {}): InvocationResponseDTO {
  return { session: { id: 'locations-session', operation: 'bible.setup.locations', node_key: 'bible-locations', policy: 'FULL_INTERACTIVE', status }, ...extra }
}
function store() { const result = useAIInvocationStore(); stores.push(result); return result }
beforeEach(() => { vi.resetAllMocks(); state.features.aiInvocationDebug = false; setActivePinia(createPinia()); localStorage.clear() })
afterEach(async () => {
  stores.splice(0).forEach(item => item.stopGenerationPolling())
  await flushPromises()
  vi.useRealTimers()
})

// Setup generation now has its own DAG lifecycle, covered in setupDagGeneration.
// These regressions still protect invocation polling used by the review drawer.
describe('invocation polling failure recovery', () => {
  it('polling tolerates a transient outage and still observes completion', async () => {
    vi.useFakeTimers()
    const invocation = store(), onError = vi.fn(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate, onError)
    state.api.get.mockRejectedValueOnce(new Error('transient')).mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('generating'))
    await vi.advanceTimersByTimeAsync(2600)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
    expect(onError).not.toHaveBeenCalled()
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it('reports repeated polling failure to the caller instead of polling silently forever', async () => {
    vi.useFakeTimers()
    const invocation = store(), onError = vi.fn()
    invocation.onSessionUpdate('locations-session', vi.fn(), onError)
    state.api.get.mockRejectedValue(new Error('offline'))
    invocation.openFromResponse(payload('generating'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('无法获取生成进度'))
    expect(state.api.get).toHaveBeenCalledTimes(3)
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it.each(['awaiting_acceptance', 'awaiting_commit'])('rejects an incomplete %s response instead of waiting forever', async status => {
    const invocation = store(), onError = vi.fn()
    invocation.onSessionUpdate('locations-session', vi.fn(), onError)
    invocation.openFromResponse(payload(status))
    await flushPromises()
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('缺少下一步'))
  })
  it('polls an ongoing commit until its authoritative completion', async () => {
    vi.useFakeTimers()
    const invocation = store(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate)
    state.api.get.mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('committing'))
    await vi.advanceTimersByTimeAsync(1300)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
    expect(invocation.liveAttemptLoading).toBe(false)
  })
  it('continues observing generation when the review drawer is closed', async () => {
    state.features.aiInvocationDebug = true
    vi.useFakeTimers()
    const invocation = store(), onUpdate = vi.fn()
    invocation.onSessionUpdate('locations-session', onUpdate)
    state.api.get.mockResolvedValue(payload('completed'))
    invocation.openFromResponse(payload('generating'))
    invocation.close()
    await vi.advanceTimersByTimeAsync(1300)
    expect(invocation.visible).toBe(false)
    expect(onUpdate).toHaveBeenLastCalledWith(payload('completed'))
  })
})
