import type { LLMProfile, LLMProtocol } from '@/api/llmControl'

export type LLMProtocolSelection = 'openai.chat' | 'openai.responses' | 'openai.auto' | 'anthropic' | 'gemini'
type ProtocolFields = Pick<LLMProfile, 'protocol' | 'endpoint_kind' | 'use_legacy_chat_completions'>

export const llmProtocolOptions: Array<{ label: string; value: LLMProtocolSelection }> = [
  { label: 'OpenAI C · Chat Completions', value: 'openai.chat' },
  { label: 'OpenAI R · Responses', value: 'openai.responses' },
  { label: 'OpenAI 自动（兼容旧配置）', value: 'openai.auto' },
  { label: 'Anthropic / Claude · Messages', value: 'anthropic' },
  { label: 'Gemini · generateContent', value: 'gemini' },
]

export function llmProtocolSelection(profile: {
  protocol: LLMProtocol; endpoint_kind?: LLMProfile['endpoint_kind']; use_legacy_chat_completions?: boolean
}): LLMProtocolSelection {
  if (profile.protocol !== 'openai') return profile.protocol
  if (profile.endpoint_kind === 'responses') return 'openai.responses'
  if (profile.endpoint_kind === 'chat' || profile.use_legacy_chat_completions) return 'openai.chat'
  return 'openai.auto'
}

export function llmProtocolFields(selection: LLMProtocolSelection): ProtocolFields {
  if (selection === 'anthropic' || selection === 'gemini') {
    return { protocol: selection, endpoint_kind: 'auto', use_legacy_chat_completions: false }
  }
  return {
    protocol: 'openai',
    endpoint_kind: selection === 'openai.responses' ? 'responses' : selection === 'openai.chat' ? 'chat' : 'auto',
    use_legacy_chat_completions: false,
  }
}

export function llmProtocolLabel(profile: Parameters<typeof llmProtocolSelection>[0]): string {
  return llmProtocolOptions.find(option => option.value === llmProtocolSelection(profile))!.label
}

export function llmProtocolHint(selection: LLMProtocolSelection): string {
  if (selection === 'openai.chat') return 'C 协议固定调用 /chat/completions，适用于支持聊天补全的 OpenAI 兼容服务。'
  if (selection === 'openai.responses') return 'R 协议固定调用 /responses；服务不支持时明确报错，不自动切换到 C 协议。'
  if (selection === 'openai.auto') return '旧自动模式优先 R，端点不支持时可能切换到 C；建议按服务商文档明确选择 C 或 R。'
  if (selection === 'anthropic') return '调用 Anthropic Messages 协议；请使用支持此协议的服务地址。'
  return '调用 Google Gemini generateContent / streamGenerateContent 协议。'
}
