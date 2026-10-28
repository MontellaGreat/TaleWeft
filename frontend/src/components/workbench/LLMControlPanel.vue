<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <div class="llm-panel">
    <header class="llm-header">
      <div class="llm-header-main">
        <div class="llm-title-row">
          <h3 class="llm-title">模型控制台</h3>
          <n-tag size="small" round :bordered="false">Provider Control</n-tag>
        </div>
        <p class="llm-lead">
          一个面板统一管理 <strong>OpenAI / Claude / Gemini</strong> 与所有
          <strong>OpenAI / Claude / Gemini 兼容网关</strong>。
          OpenAI 兼容服务会自动选择可用的调用方式。
        </p>
        <n-space :size="8" wrap>
          <n-tag size="small" type="info" round>
            运行中：{{ runtimeLabel }}
          </n-tag>
          <n-tag v-if="panelData?.runtime?.model" size="small" round>
            模型 {{ panelData?.runtime?.model }}
          </n-tag>
          <n-tag v-if="panelData?.runtime?.protocol" size="small" round>
            协议 {{ runtimeProtocolLabel }}
          </n-tag>
        </n-space>
      </div>
      <n-space :size="8" align="center">
        <n-button size="small" secondary :loading="loading" @click="loadPanel">刷新</n-button>
        <n-button size="small" type="primary" :loading="saving" @click="saveAll">保存配置</n-button>
      </n-space>
    </header>

    <n-alert v-if="runtimeUnavailable" type="warning" :show-icon="true" class="llm-alert">
      {{ panelData?.runtime?.reason || '当前未配置可用模型，请补全模型配置后生成。' }}
    </n-alert>

    <div v-if="panelData && panelData.config" class="llm-layout">
      <aside class="llm-sidebar">
        <div class="llm-sidebar-head">
          <div>
            <div class="llm-sidebar-title">配置档案</div>
            <div class="llm-sidebar-desc">可保存多组 endpoint，随时切换。</div>
          </div>
          <n-button size="tiny" secondary @click="addProfile">新增</n-button>
        </div>

        <div ref="sidebarListRef" class="llm-profile-list" @scroll="saveUiState">
          <button
            v-for="profile in panelData.config?.profiles || []"
            :key="profile.id"
            type="button"
            class="llm-profile-item"
            :class="{
              'is-active': profile.id === panelData.config?.active_profile_id,
              'is-selected': profile.id === selectedProfileId,
            }"
            @click="selectProfile(profile.id)"
          >
            <div class="llm-profile-name-row">
              <span class="llm-profile-name">{{ profile.name }}</span>
              <n-tag v-if="profile.id === panelData.config?.active_profile_id" size="tiny" type="success" round>
                启用中
              </n-tag>
            </div>
            <div class="llm-profile-meta">
              <span>{{ llmProtocolLabel(profile) }}</span>
              <span v-if="profile.model">· {{ profile.model }}</span>
            </div>
          </button>
        </div>
      </aside>

      <section v-if="selectedProfile" ref="editorRef" class="llm-editor" @scroll="saveUiState">
        <n-card size="small" :bordered="false" class="llm-card">
          <template #header>
            <div class="llm-card-head">
              <div>
                <div class="llm-card-title">{{ selectedProfile.name }}</div>
                <div class="llm-card-desc">协议、网关、模型、默认参数与高级透传参数。</div>
              </div>
              <n-space :size="8">
                <n-button size="small" secondary @click="duplicateSelected">复制</n-button>
                <n-button
                  size="small"
                  secondary
                  type="error"
                  :disabled="(panelData.config?.profiles || []).length <= 1"
                  @click="removeSelected"
                >
                  删除
                </n-button>
                <n-button size="small" secondary :loading="testing" @click="testSelected">测试连接</n-button>
                <n-button size="small" type="primary" ghost :loading="saving" @click="activateSelected">设为启用</n-button>
              </n-space>
            </div>
          </template>

          <div class="llm-form-grid">
            <div class="llm-field">
              <label class="llm-label">配置名称</label>
              <n-input v-model:value="selectedProfile.name" placeholder="例如：DeepSeek 生产网关" />
            </div>

            <div class="llm-field">
              <label class="llm-label">协议</label>
              <n-select v-model:value="selectedProtocol" :options="llmProtocolOptions" />
              <n-text depth="3" style="font-size: 12px">{{ llmProtocolHint(selectedProtocol) }}</n-text>
            </div>

            <div class="llm-field span-2">
              <label class="llm-label">Base URL</label>
              <n-input v-model:value="selectedProfile.base_url" placeholder="可填官方地址，也可填兼容网关地址" />
            </div>

            <div class="llm-field span-2">
              <label class="llm-label">API Key</label>
              <n-input
                v-model:value="selectedProfile.api_key"
                type="password"
                show-password-on="click"
                placeholder="本地保存；仅用于当前项目"
              />
            </div>

            <div v-if="selectedProfile.protocol === 'openai'" class="llm-field">
              <label class="llm-label">认证方式</label>
              <n-select v-model:value="selectedProfile.auth_mode" :options="authOptions" />
            </div>

            <div class="llm-field span-2">
              <label class="llm-label">模型名</label>
              <div class="llm-model-row">
                <n-select
                  v-if="fetchedModels.length"
                  v-model:show="modelListOpen"
                  :value="selectedProfile.model || null"
                  :options="fetchedModelOptions"
                  filterable
                  clearable
                  placeholder="选择模型，可输入搜索"
                  @update:value="selectedProfile.model = $event || ''"
                />
                <n-input
                  v-else
                  v-model:value="selectedProfile.model"
                  placeholder="拉取后直接选择，或手动填写模型 ID"
                  clearable
                />
                <n-button
                  secondary
                  size="small"
                  :loading="fetchingModels"
                  :disabled="!selectedProfile.api_key && selectedProfile.auth_mode !== 'none'"
                  @click="handleFetchModels"
                >
                  拉取模型
                </n-button>
              </div>
              <n-text v-if="fetchedModels.length > 0" depth="3" style="font-size: 11px">
                已拉取 {{ fetchedModels.length }} 个模型，可输入过滤或直接选择
              </n-text>
            </div>

            <div v-if="selectedProfile.protocol === 'openai'" class="llm-field span-2">
              <label class="llm-label">模型思考程度</label>
              <n-select v-model:value="selectedProfile.reasoning_effort" :options="reasoningOptionsForSelected" :disabled="reasoningCapabilityStatus === 'unavailable' && !selectedReasoningInvalid" clearable placeholder="由模型默认值决定" />
              <n-alert v-if="reasoningCapabilityMessage" type="warning" :show-icon="false" class="llm-reasoning-hint">
                {{ reasoningCapabilityMessage }}
              </n-alert>
              <n-text depth="3" style="font-size: 12px">留空表示不发送该参数、由模型默认决定。none 是显式的协议值，仅对自定义网关原样发送，实际效果由网关解释；官方模型请改用受支持的档位或留空。启用推理时省略温度参数。</n-text>
            </div>
            <div v-else class="llm-field span-2">
              <label class="llm-label">模型思考预算（token）</label>
              <n-input-number v-model:value="selectedProfile.thinking_budget_tokens" :min="0" :step="1024" clearable placeholder="由模型默认值决定" />
              <n-text depth="3" style="font-size: 12px">0 表示请求关闭；Anthropic 开启时至少 1024，且小于输出上限。</n-text>
            </div>

            <div class="llm-field">
              <label class="llm-label">默认 temperature</label>
              <n-input-number v-model:value="selectedProfile.temperature" :min="0" :max="2" :step="0.1" style="width: 100%" />
            </div>

            <div class="llm-field">
              <label class="llm-label">默认 max_tokens</label>
              <n-input-number v-model:value="selectedProfile.max_tokens" :min="1" :step="256" style="width: 100%" />
            </div>

            <div class="llm-field">
              <label class="llm-label">默认 top_p</label>
              <n-input-number v-model:value="selectedProfile.top_p" :min="0" :max="1" :step="0.05" clearable placeholder="由模型默认值决定" style="width: 100%" />
              <n-text depth="3" style="font-size: 12px">{{ parameterCapabilityHint('top_p') }}</n-text>
            </div>

            <div class="llm-field span-2">
              <label class="llm-label">停止序列（JSON 字符串数组）</label>
              <n-input v-model:value="stopSequencesText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" placeholder='["END", "\n\n"]' />
              <n-text depth="3" style="font-size: 12px">{{ parameterCapabilityHint('stop_sequences') }} {{ stopSequenceHint }} 空数组清空本配置的停止序列，序列按原文发送。</n-text>
            </div>

            <n-alert v-if="topPThinkingConflict" type="warning" class="llm-field span-2" :show-icon="false">
              当前已启用思考并显式设置 top_p；此组合不受当前协议支持，调用会明确拒绝。请清空 top_p，或清空思考程度（留空表示由模型默认值决定）。
            </n-alert>

            <div class="llm-field">
              <label class="llm-label">超时（秒）</label>
              <n-input-number v-model:value="selectedProfile.timeout_seconds" :min="1" :step="10" style="width: 100%" />
            </div>

            <div class="llm-field">
              <label class="llm-label">模型输出上限（未知可留空）</label>
              <n-input-number v-model:value="selectedProfile.max_output_tokens_limit" :min="1" />
            </div>
            <div class="llm-field">
              <label class="llm-label">模型上下文上限（未知可留空）</label>
              <n-input-number v-model:value="selectedProfile.context_window_limit" :min="1" />
            </div>
            <div class="llm-field span-2">
              <n-space>
                <n-button size="small" :loading="probing" @click="inspectCapabilities(false)">查看有效能力</n-button>
                <n-button size="small" :loading="probing" @click="inspectCapabilities(true)">读取上游模型能力</n-button>
              </n-space>
              <n-text v-if="capabilities" depth="3">
                输出 {{ capabilities.max_output_tokens ?? '未知' }} / 上下文 {{ capabilities.context_window ?? '未知' }}
                · 来源 {{ capabilities.source }} · {{ capabilities.valid ? '当前输出配置有效' : '当前输出配置超限' }}
              </n-text>
              <n-text v-if="capabilities" depth="3" style="font-size: 12px">
                {{ capabilities.protocol_id }}
                <span v-if="capabilities.requested_endpoint_kind === 'auto'">（自动选择，默认 {{ capabilities.endpoint_selection?.default || capabilities.endpoint_kind }}）</span>
                · {{ capabilities.confidence }}
                <span v-for="constraint in capabilities.constraints" :key="constraint"> · {{ constraint }}</span>
              </n-text>
              <n-text depth="3" style="font-size: 12px">部分网关不返回 token 上限，请按其文档填写。未知 tokenizer 使用 UTF-8 字节保守上界作预算保护，实际 usage 以模型回传为准。</n-text>
            </div>
            <div class="llm-field">
              <label class="llm-label">账单币种</label>
              <n-input v-model:value="selectedProfile.currency" placeholder="USD / CNY" />
            </div>
            <div class="llm-field">
              <label class="llm-label">每百万输入 token 单价</label>
              <n-input v-model:value="selectedProfile.input_price_per_million" placeholder="留空表示金额未知" />
            </div>
            <div class="llm-field">
              <label class="llm-label">每百万输出 token 单价</label>
              <n-input v-model:value="selectedProfile.output_price_per_million" placeholder="留空表示金额未知" />
            </div>

            <div v-if="lastTestParameters" class="llm-field span-2">
              <label class="llm-label">最近测试实际请求回执</label>
              <pre style="white-space: pre-wrap; font-size: 12px">{{ lastTestParameters }}</pre>
            </div>

            <div class="llm-field span-2">
              <label class="llm-label">备注</label>
              <n-input v-model:value="selectedProfile.notes" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" placeholder="例如：公司网关、测试环境、带 reasoning 参数" />
            </div>
          </div>

          <n-collapse class="llm-advanced">
            <n-collapse-item name="advanced" title="高级透传参数（兼容国产模型关键入口）">
              <div class="llm-form-grid">
                <div class="llm-field span-2">
                  <label class="llm-label">extra_headers（JSON）</label>
                  <n-input v-model:value="extraHeadersText" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" placeholder='{"x-foo": "bar"}' />
                </div>
                <div class="llm-field span-2">
                  <label class="llm-label">extra_query（JSON）</label>
                  <n-input v-model:value="extraQueryText" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" placeholder='{"api-version": "2024-10-21"}' />
                </div>
                <div class="llm-field span-2">
                  <label class="llm-label">extra_body（JSON）</label>
                  <n-input v-model:value="extraBodyText" type="textarea" :autosize="{ minRows: 5, maxRows: 12 }" placeholder='{"vendor_option": "value"}' />
                </div>
              </div>
              <n-alert type="info" :show-icon="false" class="llm-tip">
                这里用于兼容特殊网关的厂商专属开关、额外 header / query。思考程度和预算请使用上方控件；扩展参数不能覆盖已声明的请求字段。
                若使用带「思考链」的模型，请确认网关会把推理与正文分离；否则正文里可能出现推理片段，可换用标准聊天模型或更新到最新版本（服务端会自动剥离常见
                <code>think</code>、<code>redacted_reasoning</code> 等标签）。
              </n-alert>
            </n-collapse-item>
          </n-collapse>
          <BookModelUsage />
        </n-card>
      </section>
    </div>

    <n-empty v-else description="加载 LLM 配置中…" class="llm-empty" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  llmControlApi,
  type LLMControlConfig,
  type LLMControlPanelData,
  type LLMPreset,
  type LLMProfile,
  type LLMRuntimeSummary,
  type ModelItem,
} from '../../api/llmControl'
import { DEFAULT_MAX_OUTPUT_TOKENS } from '@/constants/llm'
import { readStorageJson, writeStorageJson } from '@/utils/storage'
import { formatApiError } from '../../utils/apiError'
import BookModelUsage from './BookModelUsage.vue'
import type { ModelCapabilities } from '@/api/llmControl'
import { llmProtocolFields, llmProtocolHint, llmProtocolLabel, llmProtocolOptions, llmProtocolSelection, type LLMProtocolSelection } from '@/utils/llmProtocol'

interface Props {
  scrollStateKey?: string
}

interface PanelUiState {
  selectedProfileId?: string
  editorTop?: number
  sidebarTop?: number
}

const props = withDefaults(defineProps<Props>(), {
  scrollStateKey: '',
})

const emit = defineEmits<{
  'panel-updated': [data: LLMControlPanelData]
}>()

const message = useMessage()

const panelData = ref<LLMControlPanelData | null>(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const fetchingModels = ref(false)
const probing = ref(false)
const capabilities = ref<ModelCapabilities | null>(null)
// Keep the reasoning capability separate from the visible probe result.  Editing
// an effort (or another generation parameter) invalidates the displayed receipt,
// but must not make the effort selector forget the model capability it just read.
const reasoningCapability = ref<ModelCapabilities | null>(null)
const lastTestParameters = ref('')
const fetchedModels = ref<ModelItem[]>([])
const modelListOpen = ref(false)
const selectedProfileId = ref('')
const extraHeadersText = ref('{}')
const extraQueryText = ref('{}')
const extraBodyText = ref('{}')
const stopSequencesText = ref('[]')
const editorRef = ref<HTMLElement | null>(null)
const sidebarListRef = ref<HTMLElement | null>(null)
let profileRevision = 0
let modelListRevision = 0
let testRequest = 0
let capabilityRequest = 0
let capabilityIdentityRevision = 0
let modelListRequest = 0
let saveRequest = 0
let loadRequest = 0
let disposed = false

const authOptions = [{ label: 'API Key', value: 'api_key' }, { label: '免密钥（本地或私有兼容服务）', value: 'none' }]
type ReasoningEffort = NonNullable<LLMProfile['reasoning_effort']>
interface ReasoningOption { label: string; value: ReasoningEffort; disabled?: boolean }
const reasoningOptions: ReasoningOption[] = [
  { label: '关闭', value: 'none' }, { label: '最小', value: 'minimal' },
  { label: '低', value: 'low' }, { label: '中', value: 'medium' }, { label: '高', value: 'high' },
  { label: '更高', value: 'xhigh' }, { label: '最大', value: 'max' },
]

const reasoningCapabilityStatus = computed<'configurable' | 'unavailable' | 'unknown'>(() => {
  const capability = reasoningCapability.value
  if (!capability || selectedProfile.value?.protocol !== 'openai') return 'unknown'
  const status = capability.features?.reasoning_effort
  if (status === 'unavailable') return 'unavailable'
  if (status === 'configurable' && (capability.reasoning_efforts || []).length > 0) return 'configurable'
  // Keep this tolerant of older capability responses that only exposed the
  // effort list, while treating an empty/model-dependent response as unknown.
  if (!status && (capability.reasoning_efforts || []).length > 0) return 'configurable'
  return 'unknown'
})

const reasoningAllowedEfforts = computed(() => {
  const supported = new Set(reasoningCapability.value?.reasoning_efforts || [])
  return reasoningOptions.filter(option => option.value !== 'none' && supported.has(option.value)).map(option => option.value)
})

const selectedReasoningInvalid = computed(() => {
  const value = selectedProfile.value?.reasoning_effort
  if (!value || selectedProfile.value?.protocol !== 'openai') return false
  if (reasoningCapabilityStatus.value === 'unavailable') return true
  return reasoningCapabilityStatus.value === 'configurable' && !reasoningAllowedEfforts.value.includes(value)
})

const reasoningOptionsForSelected = computed<ReasoningOption[]>(() => {
  const status = reasoningCapabilityStatus.value
  const selected = selectedProfile.value?.reasoning_effort
  let options = reasoningOptions
  if (status === 'configurable') {
    options = reasoningOptions.filter(option => reasoningAllowedEfforts.value.includes(option.value))
  } else if (status === 'unavailable') {
    options = []
  }
  if (selected && !options.some(option => option.value === selected)) {
    const selectedOption = reasoningOptions.find(option => option.value === selected)
    if (selectedOption) {
      options = [...options, { ...selectedOption, label: `${selectedOption.label}（当前模型不支持）`, disabled: true }]
    }
  }
  return options
})

const reasoningCapabilityMessage = computed(() => {
  const status = reasoningCapabilityStatus.value
  if (status === 'unavailable') {
    return selectedReasoningInvalid.value
      ? '当前模型不支持思考程度；已保留原草稿值，请清空该字段后再保存或测试。'
      : '当前模型不支持思考程度参数，保存和调用时不会发送该字段。'
  }
  if (status === 'configurable' && selectedReasoningInvalid.value) {
    return `当前模型仅声明支持：${reasoningAllowedEfforts.value.join('、')}；已保留原草稿值，请改为支持的档位或清空后再保存或测试。`
  }
  if (status === 'unknown') {
    return '模型思考能力尚未确认，当前保留所有档位；保存和测试时仍会由服务端校验。'
  }
  return ''
})

const selectedProfile = computed(() =>
  panelData.value?.config?.profiles.find((profile) => profile.id === selectedProfileId.value) || null
)

const selectedProtocol = computed<LLMProtocolSelection>({
  get: () => selectedProfile.value ? llmProtocolSelection(selectedProfile.value) : 'openai.auto',
  set: selection => {
    if (selectedProfile.value) Object.assign(selectedProfile.value, llmProtocolFields(selection))
  },
})

const runtimeProtocolLabel = computed(() => {
  const runtime = panelData.value?.runtime
  if (!runtime?.protocol) return ''
  const active = panelData.value?.config.profiles.find(profile => profile.id === runtime.active_profile_id)
  return llmProtocolLabel({ protocol: runtime.protocol,
    endpoint_kind: runtime.endpoint_kind ?? active?.endpoint_kind,
    use_legacy_chat_completions: runtime.endpoint_kind ? false : active?.use_legacy_chat_completions })
})

const runtimeUnavailable = computed(() => {
  const runtime = panelData.value?.runtime
  return runtime?.status === 'unavailable' || runtime?.source === 'unavailable' || runtime?.using_mock === true
})
const runtimeLabel = computed(() => {
  const runtime = panelData.value?.runtime
  if (!runtime) return '未加载'
  if (runtimeUnavailable.value) return '模型配置不可用'
  return runtime.active_profile_name || '已配置'
})

const fetchedModelOptions = computed(() =>
  fetchedModels.value.map((m) => ({
    label: m.id,
    value: m.id,
  }))
)

function parameterCapabilityHint(parameter: 'top_p' | 'stop_sequences'): string {
  const support = capabilities.value?.features?.[parameter]
  if (support === 'unavailable') return '当前能力声明不支持此参数；填写后调用会返回明确错误。'
  if (support === 'configurable' || support === 'supported') return '当前能力声明支持此参数；具体模型仍以实际调用回执为准。'
  return '支持情况尚未确认，请查看有效能力；参数会提交给服务端校验。'
}

function selectedReasoningValidationMessage(): string | null {
  if (!selectedProfile.value || !selectedReasoningInvalid.value) return null
  if (reasoningCapabilityStatus.value === 'unavailable') {
    return '当前模型不支持思考程度，请清空该字段后重试。'
  }
  return `当前模型不支持“${selectedProfile.value.reasoning_effort}”，可选：${reasoningAllowedEfforts.value.join('、')}，或清空后由模型默认决定。`
}

function validateSelectedReasoning(): boolean {
  const detail = selectedReasoningValidationMessage()
  if (!detail) return true
  message.error(`思考程度配置无效：${detail}`)
  return false
}

const topPThinkingConflict = computed(() => selectedProfile.value?.top_p != null && (
  (selectedProfile.value.protocol === 'openai' && selectedProfile.value.reasoning_effort != null && selectedProfile.value.reasoning_effort !== 'none')
  || (selectedProfile.value.protocol === 'anthropic' && (selectedProfile.value.thinking_budget_tokens ?? 0) > 0)
))
const stopSequenceHint = computed(() => {
  const profile = selectedProfile.value
  if (profile?.protocol === 'gemini') return 'Gemini 最多 5 项。'
  if (profile?.protocol !== 'openai') return '最多 16 项，每项最多 1024 个字符。'
  return '最多 4 项；配置非空停止序列时自动选择支持此参数的调用方式。'
})

async function handleFetchModels() {
  if (!selectedProfile.value) return
  try {
    commitAdvancedEditors()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return
  }
  const profile = deepClone(selectedProfile.value)
  const revision = modelListRevision
  const request = ++modelListRequest
  const isCurrent = () => !disposed && request === modelListRequest && revision === modelListRevision
  fetchingModels.value = true
  modelListOpen.value = false
  fetchedModels.value = []
  try {
    const result = await llmControlApi.fetchModels({
      profile_id: profile.id,
      protocol: profile.protocol,
      base_url: profile.base_url,
      api_key: profile.api_key,
      auth_mode: profile.auth_mode,
      extra_headers: profile.extra_headers,
      extra_query: profile.extra_query,
    })
    if (!isCurrent()) return
    if (result.success && result.items.length > 0) {
      fetchedModels.value = result.items
      modelListOpen.value = true
      message.success(`成功拉取 ${result.count} 个模型`)
    } else {
      message.warning('未获取到可用模型列表')
    }
  } catch (error: unknown) {
    if (!isCurrent()) return
    message.error(`模型列表拉取失败：${formatApiError(error, '拉取失败')}`)
  } finally {
    if (isCurrent()) fetchingModels.value = false
  }
}

function getUiStateStorageKey(): string {
  return props.scrollStateKey ? `plotpilot.llm-panel.ui.${props.scrollStateKey}` : ''
}

function readUiState(): PanelUiState {
  const key = getUiStateStorageKey()
  if (!key) return {}
  return readStorageJson<PanelUiState>(key, {}, 'session')
}

function saveUiState() {
  const key = getUiStateStorageKey()
  if (!key) return
  const payload: PanelUiState = {
    selectedProfileId: selectedProfileId.value || undefined,
    editorTop: editorRef.value?.scrollTop || 0,
    sidebarTop: sidebarListRef.value?.scrollTop || 0,
  }
  writeStorageJson(key, payload, 'session')
}

function restoreUiState() {
  const state = readUiState()
  if (editorRef.value && typeof state.editorTop === 'number') {
    editorRef.value.scrollTop = state.editorTop
  }
  if (sidebarListRef.value && typeof state.sidebarTop === 'number') {
    sidebarListRef.value.scrollTop = state.sidebarTop
  }
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function profileCapabilityIdentity(profile: LLMProfile | null): string {
  if (!profile) return ''
  return JSON.stringify({
    id: profile.id,
    protocol: profile.protocol,
    model: (profile.model || '').trim(),
    base_url: (profile.base_url || '').trim().replace(/\/$/, ''),
    endpoint_kind: profile.endpoint_kind || 'auto',
  })
}

const selectedProfileCapabilityIdentity = computed(() => profileCapabilityIdentity(selectedProfile.value))

/**
 * Capabilities are queried with reasoning disabled.  This is deliberately a
 * throwaway snapshot: an old/unsupported saved effort must not prevent the UI
 * from reading the capability matrix needed to explain that value.
 */
function capabilityQueryProfile(profile: LLMProfile): LLMProfile {
  const query = deepClone(profile)
  query.reasoning_effort = null
  if (query.extra_body && typeof query.extra_body === 'object') {
    const blockedKeys = new Set(['reasoning', 'reasoning_effort', 'reasoningEffort'])
    query.extra_body = Object.fromEntries(
      Object.entries(query.extra_body).filter(([key]) => !blockedKeys.has(key)),
    )
    const generation = query.extra_body.generationConfig
    if (generation && typeof generation === 'object' && !Array.isArray(generation)) {
      query.extra_body.generationConfig = Object.fromEntries(
        Object.entries(generation).filter(([key]) => key !== 'reasoning' && key !== 'reasoningConfig'),
      )
    }
  }
  return query
}

function acceptReasoningCapability(data: ModelCapabilities, profile: LLMProfile): boolean {
  // A late response from a previous model must not become the capability of the
  // current draft.  Gate on the echoed model when the endpoint provides it.
  if (data.model && profile.model && data.model.trim() !== profile.model.trim()) return false
  reasoningCapability.value = deepClone(data)
  return true
}

function prettyJson(value: Record<string, unknown>): string {
  return JSON.stringify(value || {}, null, 2)
}

function newProfileId(): string {
  return globalThis.crypto?.randomUUID?.() || `profile-${Date.now()}`
}

function buildEmptyProfile(): LLMProfile {
  return {
    id: newProfileId(),
    name: '新配置',
    preset_key: 'custom-openai-compatible',
    protocol: 'openai',
    base_url: '',
    api_key: '',
    model: '',
    temperature: 0.7,
    top_p: null,
    stop_sequences: [],
    max_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: 300,
    extra_headers: {},
    extra_query: {},
    extra_body: {},
    notes: '',
    use_legacy_chat_completions: false,
    endpoint_kind: 'auto',
    auth_mode: 'api_key',
    reasoning_effort: null,
    thinking_budget_tokens: null,
    max_output_tokens_limit: null,
    context_window_limit: null,
    currency: 'USD',
    input_price_per_million: null,
    output_price_per_million: null,
  }
}

function defaultRuntimeSummary(): LLMRuntimeSummary {
  return {
    source: 'unavailable',
    status: 'unavailable',
    active_profile_id: null,
    active_profile_name: null,
    protocol: null,
    model: null,
    base_url: null,
    using_mock: false,
    reason: '尚未加载或未返回运行时摘要',
  }
}

/**
 * 兼容代理/网关返回的 `{ data: { config, ... } }`，并补全缺失字段，避免访问 `config.active_profile_id` 时报错。
 */
function normalizePanelData(raw: unknown): LLMControlPanelData {
  let root: Record<string, unknown> =
    raw && typeof raw === 'object' ? { ...(raw as Record<string, unknown>) } : {}

  const wrapped = root.data
  if (
    wrapped &&
    typeof wrapped === 'object' &&
    'config' in (wrapped as object) &&
    !('config' in root)
  ) {
    root = { ...(wrapped as Record<string, unknown>) }
  }

  const cfgIn = root.config
  const cfgObj =
    cfgIn && typeof cfgIn === 'object'
      ? (cfgIn as Record<string, unknown>)
      : { version: 1, active_profile_id: null, profiles: [] }

  let profiles: LLMProfile[] = Array.isArray(cfgObj.profiles)
    ? (cfgObj.profiles as LLMProfile[]).filter(
        (p) => p && typeof p === 'object' && typeof p.id === 'string',
      )
    : []

  if (!profiles.length) {
    profiles = [buildEmptyProfile()]
  }
  profiles = profiles.map((profile) => ({ ...profile,
    top_p: profile.top_p ?? null,
    stop_sequences: profile.stop_sequences ?? [],
    ...llmProtocolFields(llmProtocolSelection(profile)),
    auth_mode: profile.auth_mode || 'api_key',
    reasoning_effort: profile.reasoning_effort ?? null,
    thinking_budget_tokens: profile.thinking_budget_tokens ?? null,
  }))

  let activeId =
    typeof cfgObj.active_profile_id === 'string' ? cfgObj.active_profile_id : null
  if (!activeId || !profiles.some((p) => p.id === activeId)) {
    activeId = profiles[0]?.id ?? null
  }

  const config: LLMControlConfig = {
    version: typeof cfgObj.version === 'number' ? cfgObj.version : 1,
    active_profile_id: activeId,
    ...(cfgObj.endpoint_mode === 'unified' || cfgObj.endpoint_mode === 'independent' ? { endpoint_mode: cfgObj.endpoint_mode } : {}),
    profiles,
  }

  const presets: LLMPreset[] = Array.isArray(root.presets)
    ? (root.presets as LLMPreset[])
    : []

  const rtIn = root.runtime
  const runtime: LLMRuntimeSummary =
    rtIn && typeof rtIn === 'object'
      ? { ...defaultRuntimeSummary(), ...(rtIn as Partial<LLMRuntimeSummary>) }
      : defaultRuntimeSummary()

  return { config, presets, runtime }
}

function uniqueProfileName(baseName: string): string {
  const normalized = baseName.trim() || '新配置'
  const names = new Set((panelData.value?.config?.profiles || []).map((profile) => profile.name))
  if (!names.has(normalized)) return normalized
  let index = 2
  while (names.has(`${normalized} ${index}`)) {
    index += 1
  }
  return `${normalized} ${index}`
}

function syncJsonEditors() {
  extraHeadersText.value = prettyJson((selectedProfile.value?.extra_headers || {}) as Record<string, unknown>)
  extraQueryText.value = prettyJson((selectedProfile.value?.extra_query || {}) as Record<string, unknown>)
  extraBodyText.value = prettyJson((selectedProfile.value?.extra_body || {}) as Record<string, unknown>)
  stopSequencesText.value = JSON.stringify(selectedProfile.value?.stop_sequences || [], null, 2)
}

function parseJsonObject(label: string, text: string): Record<string, unknown> {
  if (!text.trim()) return {}
  try {
    const parsed = JSON.parse(text)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new Error(`${label} 必须是 JSON 对象`)
    }
    return parsed as Record<string, unknown>
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'JSON 解析失败'
    throw new Error(`${label} 格式错误：${reason}`)
  }
}

function commitAdvancedEditors() {
  if (!selectedProfile.value) return
  const headers = parseJsonObject('extra_headers', extraHeadersText.value) as Record<string, string>
  const query = parseJsonObject('extra_query', extraQueryText.value)
  const body = parseJsonObject('extra_body', extraBodyText.value)
  let stops: unknown
  try {
    stops = JSON.parse(stopSequencesText.value.trim() || '[]')
  } catch {
    throw new Error('停止序列格式错误：请填写 JSON 字符串数组')
  }
  if (!Array.isArray(stops) || stops.some(value => typeof value !== 'string' || !value.length)) {
    throw new Error('停止序列格式错误：数组内每项必须是非空字符串')
  }
  if (stops.length > 16 || stops.some(value => value.length > 1024)) {
    throw new Error('停止序列格式错误：最多 16 项，每项最多 1024 个字符')
  }
  selectedProfile.value.extra_headers = headers
  selectedProfile.value.extra_query = query
  selectedProfile.value.extra_body = body
  selectedProfile.value.stop_sequences = stops
}

async function loadPanel() {
  const request = ++loadRequest
  const draftSignature = () => JSON.stringify({ config: panelData.value?.config, id: selectedProfileId.value,
    headers: extraHeadersText.value, query: extraQueryText.value, body: extraBodyText.value, stops: stopSequencesText.value })
  const requestedDraft = draftSignature()
  loading.value = true
  try {
    const raw = await llmControlApi.getPanel()
    if (disposed || request !== loadRequest) return
    const data = normalizePanelData(raw)
    if (panelData.value && draftSignature() !== requestedDraft) {
      // Refreshing saved state must not replace a draft edited while awaiting it.
      panelData.value.runtime = deepClone(data.runtime)
      panelData.value.presets = deepClone(data.presets)
      message.info('已刷新运行状态；刷新期间的新修改已保留，请保存配置')
    } else {
      panelData.value = deepClone(data)
      const persistedState = readUiState()
      const preferredId = persistedState.selectedProfileId || selectedProfileId.value
      const candidateId = preferredId && data.config.profiles.some((profile) => profile.id === preferredId)
        ? preferredId
        : data.config.active_profile_id || data.config.profiles[0]?.id || ''
      selectedProfileId.value = candidateId
      syncJsonEditors()
    }
    emit('panel-updated', deepClone(data))
    await nextTick()
    if (disposed || request !== loadRequest) return
    restoreUiState()
  } catch (error) {
    if (disposed || request !== loadRequest) return
    const detail = error instanceof Error ? error.message : '加载失败'
    message.error(`LLM 面板加载失败：${detail}`)
  } finally {
    if (!disposed && request === loadRequest) loading.value = false
  }
}

function selectProfile(profileId: string) {
  if (profileId !== selectedProfileId.value && !preserveEditorDraft()) return
  selectedProfileId.value = profileId
  saveUiState()
}

function preserveEditorDraft(): boolean {
  try {
    commitAdvancedEditors()
    return true
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return false
  }
}

function addProfile() {
  if (!panelData.value) return
  if (!preserveEditorDraft()) return
  const profile = buildEmptyProfile()
  profile.name = uniqueProfileName(profile.name)
  panelData.value.config.profiles.push(profile)
  selectedProfileId.value = profile.id
  syncJsonEditors()
  saveUiState()
}

function duplicateSelected() {
  if (!panelData.value || !selectedProfile.value) return
  try {
    commitAdvancedEditors()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return
  }
  const duplicated = deepClone(selectedProfile.value)
  duplicated.id = newProfileId()
  duplicated.name = `${duplicated.name}（副本）`
  panelData.value.config.profiles.push(duplicated)
  selectedProfileId.value = duplicated.id
  syncJsonEditors()
  saveUiState()
}

function removeSelected() {
  if (!panelData.value || !selectedProfile.value) return
  if (panelData.value.config.profiles.length <= 1) {
    message.warning('至少保留一个配置档案')
    return
  }
  const targetId = selectedProfile.value.id
  panelData.value.config.profiles = panelData.value.config.profiles.filter((profile) => profile.id !== targetId)
  if (panelData.value.config.active_profile_id === targetId) {
    panelData.value.config.active_profile_id = panelData.value.config.profiles[0]?.id || null
  }
  selectedProfileId.value = panelData.value.config.active_profile_id || panelData.value.config.profiles[0]?.id || ''
  syncJsonEditors()
  saveUiState()
}

async function saveAll() {
  if (!panelData.value) return
  if (!selectedProfile.value) return
  try {
    commitAdvancedEditors()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return
  }
  if (!validateSelectedReasoning()) return

  const submitted = deepClone(panelData.value.config)
  const submittedSignature = JSON.stringify(submitted)
  const editorSignature = () => JSON.stringify({ id: selectedProfileId.value, headers: extraHeadersText.value,
    query: extraQueryText.value, body: extraBodyText.value, stops: stopSequencesText.value })
  const submittedEditors = editorSignature()
  const request = ++saveRequest
  saving.value = true
  try {
    const response = await llmControlApi.saveConfig(submitted)
    if (disposed || request !== saveRequest) return
    const normalized = normalizePanelData(response)
    const changedWhileSaving = JSON.stringify(panelData.value?.config) !== submittedSignature
      || editorSignature() !== submittedEditors
    if (changedWhileSaving && panelData.value) {
      panelData.value.runtime = deepClone(normalized.runtime)
      panelData.value.presets = deepClone(normalized.presets)
    } else {
      panelData.value = deepClone(normalized)
      selectedProfileId.value = normalized.config.profiles.some((profile) => profile.id === selectedProfileId.value)
        ? selectedProfileId.value
        : normalized.config.active_profile_id || normalized.config.profiles[0]?.id || ''
      syncJsonEditors()
    }
    emit('panel-updated', deepClone(normalized))
    await nextTick()
    restoreUiState()
    saveUiState()
    message.success(changedWhileSaving ? 'LLM 配置已保存；保存期间的新修改已保留，请再次保存' : 'LLM 配置已保存')
  } catch (error) {
    if (disposed || request !== saveRequest) return
    const detail = error instanceof Error ? error.message : '保存失败'
    message.error(`保存失败：${detail}`)
  } finally {
    if (!disposed && request === saveRequest) saving.value = false
  }
}

async function activateSelected() {
  if (!panelData.value || !selectedProfile.value) return
  panelData.value.config.active_profile_id = selectedProfile.value.id
  await saveAll()
}

async function refreshAutomaticReasoningCapability(profile: LLMProfile, identity: string) {
  const request = ++capabilityRequest
  const identityRevision = capabilityIdentityRevision
  const isCurrent = () => !disposed
    && request === capabilityRequest
    && identityRevision === capabilityIdentityRevision
    && identity === selectedProfileCapabilityIdentity.value
  try {
    const data = await llmControlApi.capabilities(capabilityQueryProfile(profile), false)
    if (!isCurrent()) return
    acceptReasoningCapability(data, profile)
  } catch {
    // Automatic capability discovery is advisory.  Keep the selector usable
    // when a private gateway has no metadata endpoint; explicit probes still
    // surface errors to the user.
  }
}

async function testSelected() {
  if (!selectedProfile.value) return
  try {
    commitAdvancedEditors()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return
  }
  if (!validateSelectedReasoning()) return

  const profile = deepClone(selectedProfile.value)
  const revision = profileRevision
  const request = ++testRequest
  const isCurrent = () => !disposed && request === testRequest && revision === profileRevision
  lastTestParameters.value = ''
  testing.value = true
  try {
    const result = await llmControlApi.testProfile(profile)
    if (!isCurrent()) return
    lastTestParameters.value = JSON.stringify({
      requested: result.requested_parameters,
      submitted: result.submitted_parameters,
      provider_confirmation: {
        status: result.provider_confirmation_status ?? 'not_reported',
        parameters: result.provider_confirmed_parameters ?? {},
      },
    }, null, 2)
    if (result.ok) {
      const preview = result.preview ? ` · ${result.preview}` : ''
      const usage = result.token_usage ? ` · 输入 ${result.token_usage.input_tokens} / 输出 ${result.token_usage.output_tokens} token` : ' · usage 未回传'
      message.success(`连接成功（${result.latency_ms}ms）${preview}${usage} · 输出配置 ${result.effective_max_tokens ?? '未知'}`)
    } else {
      message.error(result.error || '连接失败')
    }
  } catch (error) {
    if (!isCurrent()) return
    const detail = error instanceof Error ? error.message : '连接失败'
    message.error(`测试失败：${detail}`)
  } finally {
    if (isCurrent()) testing.value = false
  }
}

async function inspectCapabilities(probe: boolean) {
  if (!selectedProfile.value) return
  try {
    commitAdvancedEditors()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '高级参数格式错误')
    return
  }
  const profile = capabilityQueryProfile(selectedProfile.value)
  const identity = selectedProfileCapabilityIdentity.value
  const identityRevision = capabilityIdentityRevision
  const revision = profileRevision
  const request = ++capabilityRequest
  const isIdentityCurrent = () => !disposed
    && request === capabilityRequest
    && identityRevision === capabilityIdentityRevision
    && identity === selectedProfileCapabilityIdentity.value
  capabilities.value = null
  probing.value = true
  try {
    const data = await llmControlApi.capabilities(profile, probe)
    if (!isIdentityCurrent()) return
    if (!acceptReasoningCapability(data, selectedProfile.value!)) return
    // A changed temperature/top_p/etc. keeps the model identity and therefore
    // can use the new effort matrix, but it must not receive an old output
    // limit receipt.  Only apply probe metadata to the visible result when the
    // complete request snapshot is still current.
    const visibleResultCurrent = revision === profileRevision
    if (probe && data.source === 'upstream_model_metadata') {
      if (visibleResultCurrent) {
        selectedProfile.value!.max_output_tokens_limit = data.max_output_tokens
        selectedProfile.value!.context_window_limit = data.context_window
        // Applying the metadata changes the draft and invalidates older requests.
        capabilities.value = { ...data, effective_max_tokens: selectedProfile.value!.max_tokens,
          valid: data.max_output_tokens === null || selectedProfile.value!.max_tokens <= data.max_output_tokens }
        message.success('已读取模型能力；保存配置后生效')
      }
    } else if (probe) {
      if (visibleResultCurrent) {
        capabilities.value = data
        message.info('模型元数据未提供上限，已保留已知或手动配置的能力')
      }
    } else {
      if (visibleResultCurrent) capabilities.value = data
    }
  } catch (error) {
    if (!isIdentityCurrent()) return
    message.error(formatApiError(error))
  } finally {
    if (isIdentityCurrent()) probing.value = false
  }
}

watch(selectedProfileId, () => {
  syncJsonEditors()
  saveUiState()
}, { flush: 'sync' })

watch(() => [selectedProfile.value, selectedProfile.value?.protocol] as const, ([profile, protocol], [previousProfile, previous]) => {
  if (!profile || profile !== previousProfile || protocol === previous) return
  if (protocol !== 'openai') {
    profile.reasoning_effort = null
    profile.endpoint_kind = 'auto'
    profile.auth_mode = 'api_key'
    profile.use_legacy_chat_completions = false
  } else {
    profile.thinking_budget_tokens = null
  }
  // Vendor body options cannot safely be reused across different protocols.
  profile.extra_body = {}
  extraBodyText.value = '{}'
}, { flush: 'sync' })

watch(selectedProfileCapabilityIdentity, async (identity) => {
  capabilityIdentityRevision += 1
  reasoningCapability.value = null
  const revision = capabilityIdentityRevision
  // Loading/switching a profile can synchronously update its ID and protocol.
  // Wait for that transaction before querying, while revoking old capability
  // state immediately so it cannot filter another model's draft.
  await nextTick()
  if (disposed || revision !== capabilityIdentityRevision || identity !== selectedProfileCapabilityIdentity.value) return
  const profile = selectedProfile.value
  if (!profile || profile.protocol !== 'openai' || !profile.model.trim()) return
  void refreshAutomaticReasoningCapability(deepClone(profile), identity)
}, { flush: 'sync' })

watch(() => {
  const profile = selectedProfile.value
  return JSON.stringify(profile ? {
    id: profile.id, protocol: profile.protocol, model: profile.model, base_url: profile.base_url,
    api_key: profile.api_key, endpoint_kind: profile.endpoint_kind, auth_mode: profile.auth_mode,
    use_legacy_chat_completions: profile.use_legacy_chat_completions, temperature: profile.temperature,
    top_p: profile.top_p, stop_sequences: profile.stop_sequences, stopsDraft: stopSequencesText.value,
    max_tokens: profile.max_tokens, timeout_seconds: profile.timeout_seconds,
    reasoning_effort: profile.reasoning_effort, thinking_budget_tokens: profile.thinking_budget_tokens,
    max_output_tokens_limit: profile.max_output_tokens_limit, context_window_limit: profile.context_window_limit,
    extra_headers: profile.extra_headers, extra_query: profile.extra_query, extra_body: profile.extra_body,
    headersDraft: extraHeadersText.value, queryDraft: extraQueryText.value, bodyDraft: extraBodyText.value,
  } : null)
}, () => {
  profileRevision += 1
  capabilities.value = null
  lastTestParameters.value = ''
  testing.value = false
  probing.value = false
}, { flush: 'sync' })

watch(() => {
  const profile = selectedProfile.value
  return JSON.stringify(profile ? { id: profile.id, protocol: profile.protocol, base_url: profile.base_url,
    api_key: profile.api_key, auth_mode: profile.auth_mode, extra_headers: profile.extra_headers,
    extra_query: profile.extra_query, headersDraft: extraHeadersText.value, queryDraft: extraQueryText.value } : null)
}, () => {
  modelListRevision += 1
  modelListOpen.value = false
  fetchedModels.value = []
  fetchingModels.value = false
}, { flush: 'sync' })

onMounted(() => {
  void loadPanel()
})

onBeforeUnmount(() => {
  disposed = true
  saveUiState()
})
</script>

<style scoped>
.llm-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--app-surface);
}

.llm-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--taleweft-split-border);
  flex-shrink: 0;
}

.llm-header-main {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.llm-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.llm-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.llm-lead {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--n-text-color-3);
}

.llm-alert {
  margin: 0 18px 12px;
  flex-shrink: 0;
}

.llm-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 14px;
  padding: 0 18px 18px;
  overflow: hidden;
}

.llm-sidebar,
.llm-editor {
  min-height: 0;
}

.llm-sidebar {
  border: 1px solid var(--taleweft-split-border);
  border-radius: 12px;
  background: var(--taleweft-panel-muted);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.llm-sidebar-head {
  padding: 14px;
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--taleweft-split-border);
}

.llm-sidebar-title {
  font-size: 13px;
  font-weight: 600;
}

.llm-sidebar-desc {
  font-size: 12px;
  color: var(--n-text-color-3);
  margin-top: 4px;
}

.llm-profile-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.llm-profile-item {
  width: 100%;
  border: 1px solid var(--profile-item-border, var(--taleweft-split-border));
  border-radius: 10px;
  background: var(--profile-item-bg, var(--app-surface));
  padding: 10px 12px;
  text-align: left;
  cursor: pointer;
  transition: all 0.16s ease;
}

.llm-profile-item:hover,
.llm-profile-item.is-selected {
  border-color: var(--profile-selected-border, var(--color-brand));
  transform: translateY(-1px);
  box-shadow: var(--app-shadow-sm);
}

.llm-profile-item.is-active {
  background: var(--profile-active-bg, var(--color-brand-light));
  border-color: var(--profile-selected-border, var(--color-brand-hover));
}

.llm-profile-name-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.llm-profile-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--profile-name-color, var(--app-text-primary));
}

.llm-profile-meta {
  margin-top: 6px;
  font-size: 12px;
  color: var(--profile-meta-color, var(--app-text-muted));
}

.llm-editor {
  overflow-y: auto;
}

.llm-card {
  min-height: 100%;
}

.llm-card-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}

.llm-card-title {
  font-size: 15px;
  font-weight: 600;
}

.llm-card-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--n-text-color-3);
}

.llm-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.llm-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.llm-field.span-2 {
  grid-column: span 2;
}

.llm-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--n-text-color-2);
}

.llm-model-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: start;
}

.llm-advanced {
  margin-top: 18px;
}

.llm-tip {
  margin-top: 10px;
}

.llm-empty {
  margin-top: 80px;
}

@media (max-width: 1180px) {
  .llm-layout {
    grid-template-columns: 1fr;
  }
}
</style>
