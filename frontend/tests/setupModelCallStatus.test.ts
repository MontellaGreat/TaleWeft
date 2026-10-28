import { afterEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import SetupModelCallStatus from '@/components/onboarding/SetupModelCallStatus.vue'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())
describe('setup model transport progress', () => {
  it('shows the actual review and counts down a server-directed cooldown', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-24T12:00:00Z'))
    const wrapper = mount(SetupModelCallStatus, { props: { progress: { purpose: 'anti_ai_semantic_review', status: 'retry_wait', call_number: 1, max_calls: 2, retry_at: Date.now() / 1000 + 120 } } })
    expect(wrapper.text()).toContain('AI 味语义审稿：上游暂时未响应，等待 120 秒后自动重试')
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.text()).toContain('等待 117 秒')
    await wrapper.setProps({ progress: { purpose: 'anti_ai_semantic_review', status: 'streaming', call_number: 2, max_calls: 2 } })
    expect(wrapper.text()).toContain('等待模型响应（2/2）')
    expect(wrapper.text()).not.toContain('正在流式接收')
    expect(wrapper.text()).not.toContain('等待 117 秒')
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows server-reported elapsed time, received characters and the current request timeout', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-24T12:00:00Z'))
    const startedAt = Date.now() / 1000 - 45
    const wrapper = mount(SetupModelCallStatus, { props: { progress: { purpose: 'deviation_review', status: 'streaming', call_number: 1, max_calls: 2, started_at: startedAt, received_characters: 0, timeout_seconds: 240 } } })

    expect(wrapper.text()).toContain('偏离检测：等待模型响应（1/2）')
    expect(wrapper.text()).toContain('已等待 45 秒')
    expect(wrapper.text()).toContain('已收到 0 字符')
    expect(wrapper.text()).toContain('本次请求时限 240 秒')
    expect(wrapper.text()).not.toContain('正在接收模型结果')
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.text()).toContain('已等待 48 秒')

    await wrapper.setProps({ progress: { purpose: 'deviation_review', status: 'streaming', call_number: 1, max_calls: 2, started_at: startedAt, received_characters: 128, last_content_at: Date.now() / 1000, timeout_seconds: 240 } })
    expect(wrapper.text()).toContain('正在接收模型结果（1/2）')
    expect(wrapper.text()).toContain('已收到 128 字符')
    expect(wrapper.text()).toContain('已等待 48 秒')
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('refreshes elapsed time when a new request starts with the same streaming status', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-24T12:00:00Z'))
    const wrapper = mount(SetupModelCallStatus, { props: { progress: { purpose: 'deviation_review', status: 'streaming', call_number: 1, max_calls: 2, started_at: Date.now() / 1000 - 45, received_characters: 150, timeout_seconds: 240 } } })
    await vi.advanceTimersByTimeAsync(1500)
    await wrapper.setProps({ progress: { purpose: 'anti_ai_semantic_review', status: 'streaming', call_number: 2, max_calls: 2, started_at: Date.now() / 1000 - 2, received_characters: 0, timeout_seconds: 600 } })

    expect(wrapper.text()).toContain('AI 味语义审稿：等待模型响应（2/2）')
    expect(wrapper.text()).toContain('已等待 2 秒')
    expect(wrapper.text()).toContain('已收到 0 字符')
    expect(wrapper.text()).toContain('本次请求时限 600 秒')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.text()).toContain('已等待 3 秒')
    expect(vi.getTimerCount()).toBe(1)

    await wrapper.setProps({ progress: { purpose: 'anti_ai_semantic_review', status: 'timed_out', call_number: 2, max_calls: 2 } })
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('only reports result reuse when the backend marks a completed result as reused', async () => {
    const wrapper = mount(SetupModelCallStatus, { props: { progress: { purpose: 'anti_ai_semantic_review', status: 'completed', call_number: 1, max_calls: 2 } } })
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    await wrapper.setProps({ progress: { purpose: 'anti_ai_semantic_review', status: 'completed', call_number: 1, max_calls: 2, reused: true } })

    expect(wrapper.text()).toContain('AI 味语义审稿：复用已完成模型结果，继续校验')
    expect(wrapper.text()).toContain('审核通过后才能保存')
    expect(wrapper.text()).not.toContain('审核已通过')
    expect(wrapper.text()).not.toContain('已保存')
    await wrapper.setProps({ progress: { purpose: 'anti_ai_semantic_review', status: 'failed', call_number: 1, max_calls: 2, reused: true } })
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('does not leave an automatic retry promise after failure or cancellation', async () => {
    const wrapper = mount(SetupModelCallStatus, { props: { progress: { purpose: 'deviation_review', status: 'failed', call_number: 2, max_calls: 2 } } })
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    await wrapper.setProps({ progress: { purpose: 'deviation_review', status: 'cancelled', call_number: 1, max_calls: 2 } })
    expect(wrapper.text()).toBe('')
    await wrapper.setProps({ progress: { purpose: 'deviation_review', status: 'timed_out', call_number: 1, max_calls: 2 } })
    expect(wrapper.text()).toBe('')
    wrapper.unmount()
  })
})
