<template>
  <div class="em">
    <header class="em-head">
      <div class="em-head-copy">
        <h4>模型引擎</h4>
        <p>多角色端点配置；统一或独立 API Key。保存后立即切换路由通道。</p>
      </div>
      <n-button type="primary" size="small" :loading="saving" @click="handleSave">保存配置</n-button>
    </header>

    <section class="em-mode" :class="{ 'em-mode--unified': isUnifiedMode }">
      <div class="em-mode-copy">
        <span class="em-mode-badge">{{ isUnifiedMode ? '统一端点' : '独立端点' }}</span>
        <p v-if="isUnifiedMode">所有场景共用一组 Base URL、API Key 与模型 ID。</p>
        <p v-else>主力 / 经济 / 知识图谱可分别配置端点与密钥。</p>
      </div>
      <n-switch v-model:value="isUnifiedMode" size="medium">
        <template #checked>统一</template>
        <template #unchecked>独立</template>
      </n-switch>
    </section>

    <div v-if="!isUnifiedMode" class="em-role-tabs">
      <button
        v-for="tab in independentTabs"
        :key="tab.key"
        type="button"
        class="em-role-tab"
        :class="{ active: activeRole === tab.key }"
        @click="activeRole = tab.key"
      >
        <strong>{{ tab.label }}</strong>
        <span>{{ tab.hint }}</span>
      </button>
    </div>

    <div class="em-body">
      <article
        v-show="isUnifiedMode || activeRole === 'main'"
        class="em-role-card em-role-card--main"
      >
        <div class="em-role-head">
          <span class="em-role-title">主力模型</span>
          <span class="em-role-tag">写作 · 分析 · 规划</span>
        </div>
        <endpoint-grid
          v-model:provider="formData.default_model_provider"
          v-model:api-key="formData.default_model_api_key"
          v-model:base-url="formData.default_model_base_url"
          v-model:model="formData.default_model"
          base-url-placeholder="例如 https://api.openai.com/v1 或兼容网关"
          model-placeholder="网关文档中的主模型 ID"
        />
        <inference-collapse
          v-model:temperature="formData.default_temperature"
          v-model:max-tokens="formData.default_max_tokens"
          v-model:timeout-seconds="formData.default_timeout_seconds"
          :protocol="formData.default_model_provider"
          v-model:reasoning-effort="formData.default_reasoning_effort"
          v-model:thinking-budget="formData.default_thinking_budget"
        />
      </article>

      <article
        v-show="!isUnifiedMode && activeRole === 'cheap'"
        class="em-role-card em-role-card--cheap"
      >
        <div class="em-role-head">
          <span class="em-role-title">经济模型</span>
          <span class="em-role-tag">批量 · 嵌入</span>
        </div>
        <endpoint-grid
          v-model:provider="formData.cheap_model_provider"
          v-model:api-key="formData.cheap_model_api_key"
          v-model:base-url="formData.cheap_model_base_url"
          v-model:model="formData.cheap_model"
          base-url-placeholder="留空则跟随主力模型"
          model-placeholder="轻量 / 低成本模型 ID"
        />
        <inference-collapse
          v-model:temperature="formData.cheap_temperature"
          v-model:max-tokens="formData.cheap_max_tokens"
          v-model:timeout-seconds="formData.cheap_timeout_seconds"
          :protocol="formData.cheap_model_provider"
          v-model:reasoning-effort="formData.cheap_reasoning_effort"
          v-model:thinking-budget="formData.cheap_thinking_budget"
        />
      </article>

      <article
        v-show="!isUnifiedMode && activeRole === 'kg'"
        class="em-role-card em-role-card--kg"
      >
        <div class="em-role-head">
          <span class="em-role-title">知识图谱</span>
          <span class="em-role-tag">三元组抽取</span>
        </div>
        <endpoint-grid
          v-model:provider="formData.knowledge_model_provider"
          v-model:api-key="formData.knowledge_model_api_key"
          v-model:base-url="formData.knowledge_model_base_url"
          v-model:model="formData.knowledge_model"
          base-url-placeholder="留空则跟随主力模型"
          model-placeholder="需较强指令遵循与结构化输出"
        />
        <inference-collapse
          v-model:temperature="formData.knowledge_temperature"
          v-model:max-tokens="formData.knowledge_max_tokens"
          v-model:timeout-seconds="formData.knowledge_timeout_seconds"
          :protocol="formData.knowledge_model_provider"
          v-model:reasoning-effort="formData.knowledge_reasoning_effort"
          v-model:thinking-budget="formData.knowledge_thinking_budget"
        />
      </article>
    </div>

    <GlobalLLMEntryButton appearance="sidebar" />
    <p class="em-foot-note">密钥仅存于本地配置，不会写入作品数据。修改后请点击「保存配置」生效。</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { llmControlApi, type LLMControlPanelData, type LLMProfile } from '@/api/llmControl'
import { llmProtocolFields, llmProtocolSelection, type LLMProtocolSelection } from '@/utils/llmProtocol'
import { DEFAULT_MAX_OUTPUT_TOKENS } from '@/constants/llm'
import EndpointGrid from './EngineMatrixEndpointGrid.vue'
import InferenceCollapse from './EngineMatrixInferenceCollapse.vue'
import GlobalLLMEntryButton from '@/components/global/GlobalLLMEntryButton.vue'

const message = useMessage()
const saving = ref(false)
const isUnifiedMode = ref(true)
const activeRole = ref<'main' | 'cheap' | 'kg'>('main')

const independentTabs = [
  { key: 'main' as const, label: '主力', hint: '写作' },
  { key: 'cheap' as const, label: '经济', hint: '批量' },
  { key: 'kg' as const, label: '图谱', hint: '抽取' },
]

const ROLE_MAIN = '主力模型'
const ROLE_CHEAP = '经济模型'
const ROLE_KG = '知识图谱模型'

interface ModelRoleConfig {
  default_model_provider: LLMProtocolSelection
  default_model_api_key: string
  default_model_base_url: string
  default_model: string
  default_temperature: number
  default_max_tokens: number
  default_timeout_seconds: number
  default_reasoning_effort: LLMProfile['reasoning_effort']
  default_thinking_budget: number | null
  cheap_model_provider: LLMProtocolSelection
  cheap_model_api_key: string
  cheap_model_base_url: string
  cheap_model: string
  cheap_temperature: number
  cheap_max_tokens: number
  cheap_timeout_seconds: number
  cheap_reasoning_effort: LLMProfile['reasoning_effort']
  cheap_thinking_budget: number | null
  knowledge_model_provider: LLMProtocolSelection
  knowledge_model_api_key: string
  knowledge_model_base_url: string
  knowledge_model: string
  knowledge_temperature: number
  knowledge_max_tokens: number
  knowledge_timeout_seconds: number
  knowledge_reasoning_effort: LLMProfile['reasoning_effort']
  knowledge_thinking_budget: number | null
}

const formData = reactive<ModelRoleConfig>({
  default_model_provider: 'openai.chat',
  default_model_api_key: '',
  default_model_base_url: '',
  default_model: '',
  default_temperature: 0.7,
  default_max_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
  default_timeout_seconds: 300,
  default_reasoning_effort: null,
  default_thinking_budget: null,
  cheap_model_provider: 'openai.chat',
  cheap_model_api_key: '',
  cheap_model_base_url: '',
  cheap_model: '',
  cheap_temperature: 0.5,
  cheap_max_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
  cheap_timeout_seconds: 300,
  cheap_reasoning_effort: null,
  cheap_thinking_budget: null,
  knowledge_model_provider: 'openai.chat',
  knowledge_model_api_key: '',
  knowledge_model_base_url: '',
  knowledge_model: '',
  knowledge_temperature: 0.3,
  knowledge_max_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
  knowledge_timeout_seconds: 300,
  knowledge_reasoning_effort: null,
  knowledge_thinking_budget: null,
})

function pickMainProfile(profiles: LLMProfile[], activeId: string | null): LLMProfile | undefined {
  return profiles.find((p) => p.name === ROLE_MAIN)
    || profiles.find((p) => p.id === activeId)
    || profiles[0]
}

function pickCheapProfile(profiles: LLMProfile[]): LLMProfile | undefined {
  return profiles.find((p) => p.name === ROLE_CHEAP)
    || profiles.find((p) => p.name.includes('经济') && (p.name.includes('模型') || p.name.toLowerCase().includes('cheap')))
}

function pickKgProfile(profiles: LLMProfile[]): LLMProfile | undefined {
  return profiles.find((p) => p.name === ROLE_KG)
    || profiles.find((p) => p.name.includes('知识') && p.name.includes('图谱'))
}

function applyProfileToForm(prefix: 'default' | 'cheap' | 'knowledge', p: LLMProfile | undefined) {
  if (!p) return
  if (prefix === 'default') {
    formData.default_model_provider = llmProtocolSelection(p)
    formData.default_model_api_key = p.api_key
    formData.default_model_base_url = p.base_url
    formData.default_model = p.model
    formData.default_temperature = p.temperature
    formData.default_max_tokens = p.max_tokens
    formData.default_timeout_seconds = p.timeout_seconds
    formData.default_reasoning_effort = p.reasoning_effort ?? null
    formData.default_thinking_budget = p.thinking_budget_tokens ?? null
    return
  }
  if (prefix === 'cheap') {
    formData.cheap_model_provider = llmProtocolSelection(p)
    formData.cheap_model_api_key = p.api_key
    formData.cheap_model_base_url = p.base_url
    formData.cheap_model = p.model
    formData.cheap_temperature = p.temperature
    formData.cheap_max_tokens = p.max_tokens
    formData.cheap_timeout_seconds = p.timeout_seconds
    formData.cheap_reasoning_effort = p.reasoning_effort ?? null
    formData.cheap_thinking_budget = p.thinking_budget_tokens ?? null
    return
  }
  formData.knowledge_model_provider = llmProtocolSelection(p)
  formData.knowledge_model_api_key = p.api_key
  formData.knowledge_model_base_url = p.base_url
  formData.knowledge_model = p.model
  formData.knowledge_temperature = p.temperature
  formData.knowledge_max_tokens = p.max_tokens
  formData.knowledge_timeout_seconds = p.timeout_seconds
    formData.knowledge_reasoning_effort = p.reasoning_effort ?? null
    formData.knowledge_thinking_budget = p.thinking_budget_tokens ?? null
}

async function loadData() {
  try {
    const data: LLMControlPanelData = await llmControlApi.getPanel()
    const profiles = data.config.profiles
    const main = pickMainProfile(profiles, data.config.active_profile_id)
    applyProfileToForm('default', main)
    applyProfileToForm('cheap', pickCheapProfile(profiles))
    applyProfileToForm('knowledge', pickKgProfile(profiles))
    isUnifiedMode.value = (data.config.endpoint_mode ?? 'unified') !== 'independent'
  } catch {
    message.error('读取模型配置失败，请检查连接后重新打开；当前显示的默认值不代表已保存配置')
  }
}

function buildProfilePayload(
  existing: LLMProfile | undefined,
  idFallback: string,
  name: string,
  provider: LLMProtocolSelection,
  key: string,
  url: string,
  model: string,
  temperature: number,
  maxTokens: number,
  timeoutSeconds: number,
  reasoningEffort: LLMProfile['reasoning_effort'],
  thinkingBudget: number | null,
): LLMProfile {
  const fields = llmProtocolFields(provider)
  const protocolChanged = existing && existing.protocol !== fields.protocol
  return {
    ...existing,
    id: existing?.id || idFallback,
    name,
    ...fields,
    base_url: url,
    api_key: key,
    model,
    temperature,
    max_tokens: maxTokens,
    timeout_seconds: Math.round(timeoutSeconds),
    extra_headers: existing?.extra_headers ?? {},
    extra_query: existing?.extra_query ?? {},
    extra_body: protocolChanged ? {} : existing?.extra_body ?? {},
    notes: existing?.notes ?? '',
    preset_key: protocolChanged
      ? fields.protocol === 'anthropic' ? 'claude-official' : fields.protocol === 'gemini' ? 'gemini-official' : 'custom-openai-compatible'
      : existing?.preset_key ?? 'custom-openai-compatible',
    auth_mode: fields.protocol === 'openai' ? existing?.auth_mode ?? 'api_key' : 'api_key',
    reasoning_effort: fields.protocol === 'openai' ? reasoningEffort ?? null : null,
    thinking_budget_tokens: fields.protocol === 'openai' ? null : thinkingBudget,
  }
}

const roleKeyFlag = computed(() => (isUnifiedMode.value ? 'uni' : 'ind'))

async function handleSave() {
  saving.value = true
  try {
    const data: LLMControlPanelData = await llmControlApi.getPanel()
    const profiles: LLMProfile[] = [...data.config.profiles]

    const mainExisting =
      profiles.find((p) => p.name === ROLE_MAIN)
      || profiles.find((p) => p.id === data.config.active_profile_id)
      || profiles[0]

    const mainProfile = buildProfilePayload(
      mainExisting,
      mainExisting?.id || 'main-default',
      ROLE_MAIN,
      formData.default_model_provider,
      formData.default_model_api_key,
      formData.default_model_base_url,
      formData.default_model,
      formData.default_temperature,
      formData.default_max_tokens,
      formData.default_timeout_seconds,
      formData.default_reasoning_effort,
      formData.default_thinking_budget,
    )

    const idx0 = profiles.findIndex((p) => p.id === mainProfile.id)
    if (idx0 >= 0) {
      profiles[idx0] = mainProfile
    } else {
      const iNamed = profiles.findIndex((p) => p.name === ROLE_MAIN)
      if (iNamed >= 0) profiles[iNamed] = mainProfile
      else profiles.unshift(mainProfile)
    }

    if (!isUnifiedMode.value) {
      const upsertRole = (
        name: string,
        idSeed: string,
        provider: LLMProtocolSelection,
        key: string,
        url: string,
        model: string,
        temperature: number,
        maxTokens: number,
        timeoutSeconds: number,
        reasoningEffort: LLMProfile['reasoning_effort'],
        thinkingBudget: number | null,
      ) => {
        const existingIdx = profiles.findIndex((p) => p.name === name)
        const existing = existingIdx >= 0 ? profiles[existingIdx] : undefined
        const roleProfile = buildProfilePayload(
          existing,
          existing?.id || `${idSeed}-${roleKeyFlag.value}-${Date.now()}`,
          name,
          provider,
          key,
          url,
          model,
          temperature,
          maxTokens,
          timeoutSeconds,
          reasoningEffort,
          thinkingBudget,
        )
        if (existingIdx >= 0) profiles[existingIdx] = roleProfile
        else profiles.push(roleProfile)
      }

      upsertRole(
        ROLE_CHEAP,
        'cheap',
        formData.cheap_model_provider,
        formData.cheap_model_api_key,
        formData.cheap_model_base_url,
        formData.cheap_model,
        formData.cheap_temperature,
        formData.cheap_max_tokens,
        formData.cheap_timeout_seconds,
      formData.cheap_reasoning_effort,
      formData.cheap_thinking_budget,
      )
      upsertRole(
        ROLE_KG,
        'kg',
        formData.knowledge_model_provider,
        formData.knowledge_model_api_key,
        formData.knowledge_model_base_url,
        formData.knowledge_model,
        formData.knowledge_temperature,
        formData.knowledge_max_tokens,
        formData.knowledge_timeout_seconds,
      formData.knowledge_reasoning_effort,
      formData.knowledge_thinking_budget,
      )
    } else {
      for (let i = profiles.length - 1; i >= 0; i--) {
        const n = profiles[i].name
        if (n === ROLE_MAIN) continue
        if (
          n === ROLE_CHEAP
          || n === ROLE_KG
          || (n.includes('经济') && n.includes('模型'))
          || (n.includes('知识') && n.includes('图谱'))
        ) {
          profiles.splice(i, 1)
        }
      }
    }

    const newConfig = {
      ...data.config,
      version: 1,
      endpoint_mode: (isUnifiedMode.value ? 'unified' : 'independent') as 'unified' | 'independent',
      active_profile_id: mainProfile.id,
      profiles,
    }

    await llmControlApi.saveConfig(newConfig)
    message.success('配置已保存，系统已切换路由通道')
    await loadData()
  } catch {
    message.error('保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void loadData()
})
</script>

<style scoped>
.em {
  max-width: 760px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.em-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.em-head-copy h4 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: var(--app-text-primary);
}

.em-head-copy p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--app-text-muted);
  max-width: 520px;
}

.em-mode {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--app-border);
  background: var(--app-surface);
}

.em-mode--unified {
  border-color: color-mix(in srgb, var(--color-brand) 35%, var(--app-border));
  background: color-mix(in srgb, var(--color-brand-light, rgba(37, 99, 235, 0.08)) 55%, var(--app-surface));
}

.em-mode-copy {
  flex: 1;
  min-width: 0;
}

.em-mode-badge {
  display: inline-flex;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  background: var(--app-border);
  color: var(--app-text-muted);
}

.em-mode--unified .em-mode-badge {
  background: var(--color-brand-light);
  color: var(--color-brand);
}

.em-mode-copy p {
  margin: 6px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--app-text-muted);
}

.em-role-tabs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

.em-role-tab {
  display: grid;
  gap: 2px;
  padding: 8px 6px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-surface);
  cursor: pointer;
  text-align: center;
  transition: border-color 0.15s, background 0.15s;
}

.em-role-tab:hover {
  border-color: color-mix(in srgb, var(--color-brand) 28%, var(--app-border));
}

.em-role-tab.active {
  border-color: var(--color-brand-border);
  background: var(--color-brand-light);
}

.em-role-tab strong {
  font-size: 12px;
  color: var(--app-text-primary);
}

.em-role-tab span {
  font-size: 10px;
  color: var(--app-text-muted);
}

.em-body {
  max-height: min(68vh, 640px);
  overflow-y: auto;
  padding-right: 4px;
  scrollbar-width: thin;
}

.em-role-card {
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--app-border);
  border-left-width: 3px;
  background: var(--app-surface);
}

.em-role-card--main { border-left-color: var(--color-brand); }
.em-role-card--cheap { border-left-color: var(--color-warning); }
.em-role-card--kg { border-left-color: var(--color-info, #3b82f6); }

.em-role-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.em-role-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--app-text-primary);
}

.em-role-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--app-surface-subtle);
  color: var(--app-text-muted);
}

.em-foot-note {
  margin: 0;
  font-size: 11px;
  color: var(--app-text-muted);
  line-height: 1.5;
}

@media (max-width: 560px) {
  .em-head {
    flex-direction: column;
    align-items: stretch;
  }

  .em-mode {
    flex-direction: column;
  }
}
</style>
