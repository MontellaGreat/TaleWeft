import type { InvocationResponseDTO } from '../api/aiInvocation'

/** A failed invocation must release its caller's busy state, including in headless mode. */
export function invocationFailureMessage(payload: InvocationResponseDTO): string | null {
  if (payload.commit?.status === 'failed') {
    return payload.commit.error || payload.commit.steps?.find(step => step.error)?.error || '生成结果保存失败，请重试'
  }
  const status = payload.session.status
  if (status === 'blocked') {
    const missing = payload.session.variable_plan?.required_missing
      || payload.session.prompt_snapshot?.missing_variables || []
    return missing.length ? `生成所需资料缺失：${missing.join('、')}。请先确认前面的设定。` : '生成被阻止，请检查前面的设定后重试'
  }
  if (status === 'cancelled') return '生成已取消，可以重新生成'
  if (status === 'failed') return payload.attempt?.error || 'AI 生成失败，请重试'
  return null
}
