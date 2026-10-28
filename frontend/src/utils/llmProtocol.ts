import type { LLMProfile, LLMProtocol } from '@/api/llmControl'

export type LLMProtocolSelection = 'openai.auto' | 'anthropic' | 'gemini'
type ProtocolFields = Pick<LLMProfile, 'protocol' | 'endpoint_kind' | 'use_legacy_chat_completions'>

export const llmProtocolOptions: Array<{ label: string; value: LLMProtocolSelection }> = [
  { label: 'OpenAI 自动', value: 'openai.auto' },
  { label: 'Anthropic / Claude · Messages', value: 'anthropic' },
  { label: 'Gemini · generateContent', value: 'gemini' },
]

export function llmProtocolSelection(profile: {
  protocol: LLMProtocol; endpoint_kind?: LLMProfile['endpoint_kind']; use_legacy_chat_completions?: boolean
}): LLMProtocolSelection {
  if (profile.protocol !== 'openai') return profile.protocol
  return 'openai.auto'
}

export function llmProtocolFields(selection: LLMProtocolSelection): ProtocolFields {
  if (selection === 'anthropic' || selection === 'gemini') {
    return { protocol: selection, endpoint_kind: 'auto', use_legacy_chat_completions: false }
  }
  return {
    protocol: 'openai',
    endpoint_kind: 'auto',
    use_legacy_chat_completions: false,
  }
}

export function llmProtocolLabel(profile: Parameters<typeof llmProtocolSelection>[0]): string {
  return llmProtocolOptions.find(option => option.value === llmProtocolSelection(profile))!.label
}

export function llmProtocolHint(selection: LLMProtocolSelection): string {
  if (selection === 'openai.auto') return '自动选择 Responses 或 Chat Completions，适用于 OpenAI 与兼容网关。'
  if (selection === 'anthropic') return '调用 Anthropic Messages 协议；请使用支持此协议的服务地址。'
  return '调用 Google Gemini generateContent / streamGenerateContent 协议。'
}
