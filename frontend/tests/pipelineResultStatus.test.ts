import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PipelineResultSummary from '../src/components/autopilot/PipelineResultSummary.vue'
import StoryPipelineObservability from '../src/components/autopilot/StoryPipelineObservability.vue'

vi.mock('@/composables/usePolling', () => ({ usePolling: () => ({}) }))

describe('pipeline result evidence', () => {
  it.each(['unavailable', 'deferred'] as const)('separates committed body from %s work', status => {
    const wrapper = mount(PipelineResultSummary, { props: { status: {
      pipeline_status: status, pipeline_success: true, pipeline_complete: true,
      pipeline_step_results: { run_post_commit: {
        status, passed: false, skip: false, message: '派生服务等待恢复', score: null,
        violations: [], suggestions: [], metadata: {},
      } },
    } } })
    expect(wrapper.text()).toContain('正文已提交')
    expect(wrapper.text()).toContain(status === 'unavailable' ? '不可用' : '等待后续处理')
    expect(wrapper.text()).not.toContain('检查与派生已通过')
  })

  it('shows rejection and omits completion while still executing', () => {
    const rejected = mount(PipelineResultSummary, { props: { status: {
      pipeline_status: 'failed', pipeline_success: false, pipeline_complete: true,
    } } })
    expect(rejected.text()).toContain('检查失败')
    expect(rejected.text()).toContain('正文流程未完成')
    const executing = mount(PipelineResultSummary, { props: { status: { pipeline_complete: false } } })
    expect(executing.find('[role="status"]').exists()).toBe(false)
  })

  it('never infers successful checks from a later wave or elapsed time', () => {
    const wrapper = mount(StoryPipelineObservability, { props: { status: {
      story_pipeline_wave_index: 8, story_pipeline_wave_entered_at: Date.now() / 1000 - 60,
      aftermath_live_status: 'running', pipeline_step_status: {
        save_chapter: 'passed', validate_content: 'unavailable', validate_voice: 'deferred',
      },
    } } })
    expect(wrapper.findAll('.spo-step__ok')).toHaveLength(1)
    expect(wrapper.findAll('.spo-aftermath-step--done')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('8/8 已确认')
  })
})
