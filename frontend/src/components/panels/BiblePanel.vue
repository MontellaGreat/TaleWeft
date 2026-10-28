<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <div class="bible-panel pp-panel">

    <!-- ── Header ──────────────────────────────── -->
    <header class="pp-panel-header">
      <div class="pp-panel-header-main">
        <div class="bible-title-row">
          <div class="wb-icon-badge" style="background:#4f46e5">
            <n-icon size="14"><DocumentTextOutline /></n-icon>
          </div>
          <span class="pp-panel-title">作品设定</span>
          <n-tag size="small" round :bordered="false" class="bible-badge">Story Bible</n-tag>
        </div>
        <div v-if="biblePanelDataReady" class="bible-header-stats">
          <span
            class="pp-chip"
            :class="stats.premiseOk ? 'pp-chip--success' : 'pp-chip--muted'"
            style="font-size:10px"
          >梗概 {{ stats.premiseOk ? '✓ 已填' : '待填' }}</span>
          <span
            class="pp-chip"
            :class="stats.styleOk ? 'pp-chip--success' : 'pp-chip--muted'"
            style="font-size:10px"
          >文风 {{ stats.styleOk ? '✓ 已填' : '待填' }}</span>
        </div>
        <div v-else class="bible-header-stats">
          <span class="pp-chip pp-chip--muted" style="font-size:10px;opacity:.45">梗概 …</span>
          <span class="pp-chip pp-chip--muted" style="font-size:10px;opacity:.45">文风 …</span>
        </div>
      </div>
      <div class="pp-panel-actions">
        <n-button size="small" secondary :loading="generating" :disabled="!biblePanelDataReady || saving" @click="generateBible" title="用 AI 根据小说标题重新生成设定">
          ✦ AI 生成
        </n-button>
        <n-button size="small" type="primary" :loading="saving" :disabled="!canSave" @click="save">保存</n-button>
      </div>
    </header>

    <!-- ── Scrollable body ──────────────────────── -->
    <div class="pp-panel-content bible-body">

      <n-alert v-if="loadError" type="error" :show-icon="false">{{ loadError }}</n-alert>
      <div class="pp-section">
        <div class="pp-section-header">
          <div class="wb-icon-badge" style="background:#6366f1"><n-icon size="14"><DocumentTextOutline /></n-icon></div>
          <span class="pp-section-label">创作契约</span>
          <n-tag size="tiny" :bordered="false" type="info">可修改</n-tag>
        </div>
        <div class="pp-section-body bible-contract-fields">
          <label v-for="field in contractTextFields" :key="field.key" class="bible-contract-field">
            <span class="bible-contract-card__k">{{ field.label }}</span>
            <n-input v-model:value="contract[field.key]" type="textarea" :input-props="{ 'aria-label': field.label }"
              :autosize="{ minRows: field.rows, maxRows: 12 }" :disabled="!biblePanelDataReady || saving" />
          </label>
          <div class="bible-contract-grid">
            <label class="bible-contract-field"><span class="bible-contract-card__k">目标章节数</span>
              <n-input-number v-model:value="contract.target_chapters" :input-props="{ 'aria-label': '目标章节数' }" :min="1" :max="100000" :precision="0" :disabled="!biblePanelDataReady || saving" />
            </label>
            <label class="bible-contract-field"><span class="bible-contract-card__k">每章目标字数</span>
              <n-input-number v-model:value="contract.target_words_per_chapter" :input-props="{ 'aria-label': '每章目标字数' }" :min="500" :max="20000" :precision="0" :disabled="!biblePanelDataReady || saving" />
            </label>
          </div>
          <n-checkbox v-if="requiresMarketConfirmation" v-model:checked="confirmMarketProfile" :disabled="saving">
            已核对修改后的完整五维画像
          </n-checkbox>
        </div>
      </div>
      <div class="pp-section">
        <div class="pp-section-header">
          <div class="wb-icon-badge" style="background:#6366f1"><span>文</span></div>
          <span class="pp-section-label">文风公约</span>
          <n-button size="tiny" secondary :disabled="!biblePanelDataReady || saving" @click="openStylePresetModal" style="margin-left:auto">更换文风</n-button>
        </div>
        <div class="pp-section-body">
          <n-input v-model:value="contract.style_notes" type="textarea" :input-props="{ 'aria-label': '文风公约' }"
            :autosize="{ minRows: 5, maxRows: 16 }" :disabled="!biblePanelDataReady || saving" class="bible-textarea" />
        </div>
      </div>
      <div class="pp-section">
        <div class="pp-section-header">
          <div class="wb-icon-badge" style="background:#7c3aed"><n-icon size="14"><BookmarkOutline /></n-icon></div>
          <span class="pp-section-label">梗概锁定</span>
          <n-button size="tiny" secondary :disabled="!biblePanelDataReady || saving" :loading="generatingKnowledge"
            @click="generatePremiseKnowledge" style="margin-left:auto" title="根据 Bible 生成或刷新梗概锁定">✦ AI 生成</n-button>
        </div>
        <div class="pp-section-body">
          <n-input v-model:value="contract.premise_lock" type="textarea" :input-props="{ 'aria-label': '梗概锁定' }"
            :autosize="{ minRows: 6, maxRows: 20 }" placeholder="主线、不可违背设定、结局走向（防百万字跑篇）…"
            show-count :maxlength="24000" :disabled="!biblePanelDataReady || saving" class="bible-textarea" />
        </div>
      </div>

    </div>

    <!-- ── Footer ───────────────────────────────── -->
    <footer class="pp-panel-footer">
      <span class="pp-panel-footer-note">
        <template v-if="biblePanelDataReady">
          {{ stats.premiseOk && stats.styleOk ? '创作契约已就绪' : '梗概或文风公约待完善' }}
        </template>
      </span>
      <n-space :size="4"><n-button size="small" quaternary :disabled="saving" @click="load()">重新加载</n-button><n-button size="small" quaternary :disabled="!biblePanelDataReady || saving" @click="openJsonModal">JSON 编辑器</n-button></n-space>
    </footer>

    <!-- JSON 编辑器弹窗 -->
    <n-modal v-model:show="showJsonModal" preset="card" title="创作契约 JSON 编辑器" style="width:800px;max-width:90vw">
      <n-space vertical :size="12">
        <n-input
          v-model:value="jsonRaw"
          type="textarea"
          :rows="20"
          placeholder="JSON 格式"
          class="bible-json-input"
        />
        <n-space :size="8">
          <n-button @click="formatJson">格式化</n-button>
          <n-button type="primary" :loading="saving" @click="saveFromJson">保存</n-button>
        </n-space>
      </n-space>
    </n-modal>

    <!-- 文风预设选择弹窗 -->
    <n-modal
      v-model:show="showStylePresetModal"
      preset="card"
      title="选择文风预设"
      style="width:900px;max-width:95vw"
    >
      <StylePresetSelector v-model="selectedStylePresetValue" />
      <template #footer>
        <n-space justify="end" :size="8">
          <n-button @click="showStylePresetModal = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="applyStylePreset">应用</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed, onMounted, onUnmounted } from 'vue'
import { useMessage } from 'naive-ui'
import { DocumentTextOutline, BookmarkOutline } from '@vicons/ionicons5'
import { bibleApi } from '../../api/bible'
import { knowledgeApi } from '../../api/knowledge'
import { creationContractApi, type CreationContract, type CreationContractState } from '@/api/creationContract'
import { MARKET_STYLE_PRESETS, matchPresetValue } from '@/constants/marketStylePresets'
import { formatApiError } from '@/utils/apiError'
import StylePresetSelector from './StylePresetSelector.vue'

const props = withDefaults(defineProps<{ slug: string; reloadNonce?: number }>(), { reloadNonce: 0 })
const message = useMessage()
const emptyContract = (): CreationContract => ({ locked_genre: '', locked_world_preset: '', locked_story_structure: '',
  locked_pacing_control: '', locked_writing_style: '', locked_special_requirements: '', target_chapters: 0,
  target_words_per_chapter: 2500, style_notes: '', premise_lock: '' })
const contract = ref(emptyContract())
const loadedContract = ref(emptyContract())
const contractVersion = ref('')
const structuredMarket = ref(false)
const confirmMarketProfile = ref(false)
const contractTextFields = [
  { key: 'locked_genre', label: '赛道', rows: 1 },
  { key: 'locked_world_preset', label: '世界基调', rows: 3 },
  { key: 'locked_story_structure', label: '叙事结构', rows: 3 },
  { key: 'locked_pacing_control', label: '节奏控制', rows: 3 },
  { key: 'locked_writing_style', label: '写作规则', rows: 3 },
  { key: 'locked_special_requirements', label: '硬性要求', rows: 3 },
] as const
const requiresMarketConfirmation = computed(() => structuredMarket.value && contractTextFields
  .filter(field => field.key !== 'locked_genre').some(field => contract.value[field.key] !== loadedContract.value[field.key]))
const jsonRaw = ref('')
const showJsonModal = ref(false)
const showStylePresetModal = ref(false)
const selectedStylePresetValue = ref('')
const saving = ref(false)
const generating = ref(false)
const generatingKnowledge = ref(false)
const biblePanelDataReady = ref(false)
const loadError = ref('')
const canSave = computed(() => biblePanelDataReady.value && !saving.value && (!requiresMarketConfirmation.value || confirmMarketProfile.value))
const stats = computed(() => ({ styleOk: contract.value.style_notes.trim().length >= 20, premiseOk: contract.value.premise_lock.trim().length >= 20 }))
let biblePanelLoadSeq = 0
const syncJsonFromState = () => { jsonRaw.value = JSON.stringify(contract.value, null, 2) }
function applyLoaded(data: CreationContractState) {
  contract.value = { ...data.contract }
  loadedContract.value = { ...data.contract }
  contractVersion.value = data.version
  structuredMarket.value = data.structured_market
  confirmMarketProfile.value = false
  syncJsonFromState()
}
async function load() {
  const seq = ++biblePanelLoadSeq
  const slug = props.slug
  biblePanelDataReady.value = false
  loadError.value = ''
  try {
    const data = await creationContractApi.get(slug)
    if (seq !== biblePanelLoadSeq || props.slug !== slug) return
    applyLoaded(data)
    biblePanelDataReady.value = true
  } catch (error) {
    if (seq !== biblePanelLoadSeq || props.slug !== slug) return
    loadError.value = formatApiError(error, '加载创作契约失败，请重新加载')
    message.error(loadError.value)
  }
}
async function save(): Promise<boolean> {
  if (!canSave.value) return false
  saving.value = true
  const slug = props.slug
  const seq = biblePanelLoadSeq
  try {
    const saved = await creationContractApi.save(slug, { version: contractVersion.value,
      contract: { ...contract.value }, confirm_market_profile: confirmMarketProfile.value })
    const persisted = await creationContractApi.get(slug)
    if (saved.version !== persisted.version) throw new Error('创作契约在保存后再次更新，请重新加载并核对')
    if (slug !== props.slug || seq !== biblePanelLoadSeq) return false
    applyLoaded(persisted)
    window.dispatchEvent(new CustomEvent('plotpilot:knowledge:reload'))
    window.dispatchEvent(new CustomEvent('plotpilot:novel:reload'))
    message.success('创作契约、文风与梗概已保存')
    return true
  } catch (error) {
    if (slug === props.slug) message.error(formatApiError(error, '保存失败'))
    return false
  } finally { saving.value = false }
}
async function generatePremiseKnowledge() {
  generatingKnowledge.value = true
  try {
    const result = await knowledgeApi.generateKnowledge(props.slug)
    message.success(result.message || '梗概已生成')
    await load()
    window.dispatchEvent(new CustomEvent('plotpilot:knowledge:reload'))
  } catch (error) { message.error(formatApiError(error, 'AI 生成失败，请确认 API Key 已配置')) }
  finally { generatingKnowledge.value = false }
}
async function saveFromJson() {
  try {
    const parsed = JSON.parse(jsonRaw.value)
    const valid = parsed && typeof parsed === 'object' && Object.entries(emptyContract()).every(([key, value]) => typeof parsed[key] === typeof value)
    if (!valid) throw new Error('契约字段缺失或类型错误')
    contract.value = parsed
    if (requiresMarketConfirmation.value && !confirmMarketProfile.value) {
      showJsonModal.value = false
      message.error('请在契约表单核对并确认修改后的完整五维画像，再保存')
      return
    }
    if (await save()) showJsonModal.value = false
  } catch (error) { message.error(formatApiError(error, 'JSON 格式错误')) }
}
function openJsonModal() { syncJsonFromState(); showJsonModal.value = true }
function formatJson() {
  try { jsonRaw.value = JSON.stringify(JSON.parse(jsonRaw.value), null, 2) }
  catch { message.error('JSON 格式错误，无法格式化') }
}
async function generateBible() {
  generating.value = true
  try {
    const result = await bibleApi.generateBible(props.slug)
    message.success(result.message || 'Bible 生成成功')
    await load()
  } catch (error) { message.error(formatApiError(error, 'AI 生成失败，请确认 API Key 已配置')) }
  finally { generating.value = false }
}
function openStylePresetModal() {
  selectedStylePresetValue.value = matchPresetValue(contract.value.style_notes) || MARKET_STYLE_PRESETS[0]?.value || 'xianxia_hot'
  showStylePresetModal.value = true
}
async function applyStylePreset() {
  const preset = MARKET_STYLE_PRESETS.find(item => item.value === selectedStylePresetValue.value)
  if (!preset) return
  contract.value.style_notes = preset.body
  showStylePresetModal.value = false
  await save()
}
const BIBLE_PANEL_SOFT_RELOAD = 'plotpilot:bible-panel:soft-reload'
function onBiblePanelSoftReload() { if (props.slug && !saving.value) void load() }
watch(() => [props.slug, props.reloadNonce] as const, () => { if (props.slug.trim()) void load() }, { immediate: true })
watch(() => contractTextFields.map(field => contract.value[field.key]), () => { confirmMarketProfile.value = false }, { flush: 'sync' })
onMounted(() => window.addEventListener(BIBLE_PANEL_SOFT_RELOAD, onBiblePanelSoftReload))
onUnmounted(() => window.removeEventListener(BIBLE_PANEL_SOFT_RELOAD, onBiblePanelSoftReload))
</script>

<style scoped>
/* ── Panel root (inherits pp-panel) ─────────────── */
.bible-panel {
  /* pp-panel provides: flex-col, height:100%, overflow:hidden, bg */
}

/* ── Header ──────────────────────────────────────── */
.bible-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.bible-badge {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  background: rgba(79, 70, 229, 0.1) !important;
  color: #4338ca !important;
}

.bible-header-stats {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  margin-top: 4px;
}

/* ── Body scroll area ────────────────────────────── */
.bible-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.bible-body > .pp-section { flex-shrink: 0; }
.bible-contract-fields, .bible-contract-field { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.bible-contract-fields { gap: 14px; }
.bible-contract-field :deep(.n-input-number) { width: 100%; }
.bible-panel > .pp-panel-header { flex-wrap: wrap; }
.bible-panel > .pp-panel-header .pp-panel-actions { margin-left: auto; }

/* ── 创作契约 ────────────────────────────── */
.bible-contract-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: 8px;
}

.bible-contract-card__k {
  font-size: 11px;
  font-weight: 700;
  color: var(--app-text-muted);
}

/* ── Textareas ───────────────────────────────────── */
.bible-textarea :deep(textarea) {
  line-height: 1.6;
}

/* ── JSON modal ──────────────────────────────────── */
.bible-json-input :deep(textarea) {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  line-height: 1.6;
}
</style>
