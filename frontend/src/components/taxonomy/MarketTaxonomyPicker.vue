<template>
  <div class="mtp" :class="{ 'mtp--busy': disabled }">
    <div class="mtp-section-head"><span class="mtp-k">① 读者频道</span></div>
    <div class="mtp-channel-row">
      <n-button v-for="channel in channels" :key="channel.id" round strong
        :type="selection.audienceMode === channel.id ? 'primary' : 'default'"
        :secondary="selection.audienceMode !== channel.id" :disabled="disabled"
        :data-testid="`market-channel-${channel.id}`" @click="pickAudience(channel.id)">
        {{ pickLocaleLabel(channel, locale) }}
      </n-button>
    </div>
    <p class="mtp-note">{{ activeChannel?.description || '先选男频、女频或双频，再选择对应大类与网文主题。' }}</p>

    <template v-if="selection.audienceMode">
      <n-input v-model:value="searchQuery" clearable round :disabled="disabled" data-testid="market-search"
        placeholder="搜索当前频道的大类和主题，如「电竞」「年代」「谍战」" />
      <div class="mtp-section-head">
        <span class="mtp-k">② 大类</span><span class="mtp-hint">当前频道 {{ filteredCategories.length }} 个大类</span>
      </div>
      <div class="mtp-major-row">
        <n-button v-for="category in visibleCategories" :key="category.id" size="small" round
          :type="!selection.customCategory && selection.canonicalCategoryId === category.id ? 'primary' : 'default'"
          :secondary="selection.customCategory || selection.canonicalCategoryId !== category.id" :disabled="disabled"
          :data-testid="`market-category-${category.id}`" @click="pickCategory(category)">
          {{ pickLocaleLabel(category, locale) }}
        </n-button>
        <n-button v-if="hiddenCategoryCount" size="small" text :disabled="disabled" @click="showAllCategories = true">展开其余 {{ hiddenCategoryCount }} 个大类</n-button>
        <n-button v-else-if="showAllCategories && !searchQuery.trim()" size="small" text :disabled="disabled" @click="showAllCategories = false">收起大类</n-button>
        <n-button size="small" round dashed :type="selection.customCategory ? 'primary' : 'default'" :disabled="disabled"
          data-testid="market-custom-category" @click="pickCustomCategory">＋ 自填大类
        </n-button>
      </div>
      <p v-if="!filteredCategories.length" class="mtp-note">没有匹配的内置分类，可以换关键词或自填大类。</p>
      <n-input v-if="selection.customCategory" v-model:value="customCategoryLabel" :disabled="disabled" :maxlength="80"
        placeholder="填写本书的大类名称" data-testid="market-custom-category-label" />

      <div v-if="activeCategory || selection.customCategory" class="mtp-detail">
        <div class="mtp-section-head">
          <span class="mtp-k">③ 网文主题</span>
          <span class="mtp-hint">{{ selection.customCategory ? '自定义大类使用自填主题' : `当前大类 ${filteredThemes.length} 个主题` }}</span>
        </div>
        <div class="mtp-theme-row">
          <n-button v-for="theme in visibleThemes" :key="theme.id" size="small" round
            :type="!selection.customTheme && selection.canonicalThemeId === theme.id ? 'primary' : 'default'"
            :secondary="selection.customTheme || selection.canonicalThemeId !== theme.id" :disabled="disabled"
            :data-testid="`market-theme-${theme.id}`" @click="pickTheme(theme)">
            {{ pickLocaleLabel(theme, locale) }}
          </n-button>
          <n-button v-if="hiddenThemeCount" size="small" text :disabled="disabled" @click="showAllThemes = true">展开其余 {{ hiddenThemeCount }} 个主题</n-button>
          <n-button v-else-if="showAllThemes && !searchQuery.trim()" size="small" text :disabled="disabled" @click="showAllThemes = false">收起主题</n-button>
          <n-button size="small" round dashed :type="selection.customTheme ? 'primary' : 'default'" :disabled="disabled"
            data-testid="market-custom-theme" @click="pickCustomTheme">＋ 自填主题
          </n-button>
        </div>
        <n-input v-if="selection.customTheme" v-model:value="customThemeLabel" :disabled="disabled" :maxlength="80"
          placeholder="填写本书的网文主题名称" data-testid="market-custom-theme-label" />
        <p v-else-if="!activeTheme" class="mtp-note">点击一个主题后展开对应的五维画像。</p>

        <template v-if="activeTheme || selection.customTheme">
          <div class="mtp-classify-strip">
            <div><span class="mtp-mini-label">读者频道</span><strong>{{ activeChannel ? pickLocaleLabel(activeChannel, locale) : '' }}</strong></div>
            <div><span class="mtp-mini-label">市场大类</span><strong>{{ selectionLabels.category || '待填写' }}</strong></div>
            <div><span class="mtp-mini-label">网文主题</span><strong>{{ selectionLabels.theme || '待填写' }}</strong></div>
          </div>
          <n-alert v-if="activeTheme && !builtinReady" type="info" :show-icon="false" class="mtp-profile-status">
            此主题的内置画像{{ activeTheme.status === 'legacy_review_required' ? '来自旧版，待复核' : '为待复核草稿' }}。请核对下面五项；核对后将保存为本书的独立画像。
          </n-alert>
          <div class="mtp-section-head"><span class="mtp-k">④ 五维创作画像</span><span class="mtp-hint">全部可修改，修改后需要重新核对</span></div>
          <div class="mtp-writing-grid">
            <div v-for="item in profileCards" :key="item.key" class="mtp-profile-card" :class="{ 'mtp-profile-card--world': item.key === 'world_tone' }">
              <strong>{{ item.title }}</strong><p class="mtp-note">{{ item.note }}</p>
              <n-input type="textarea" :autosize="{ minRows: item.key === 'world_tone' ? 3 : 5, maxRows: 16 }"
                v-model:value="item.model.value" :disabled="disabled" :placeholder="item.placeholder"
                :data-testid="`market-profile-${item.key}`" class="mtp-profile-input" />
            </div>
          </div>
          <div class="mtp-extra-grid">
            <div><span class="mtp-mini-label">目标平台（可选）</span><n-select v-model:value="platform" :options="platformOptions" clearable :disabled="disabled" placeholder="选择目标平台" /></div>
            <div><span class="mtp-mini-label">题材标签（可选，以顿号或逗号分隔）</span><n-input v-model:value="tagsText" :disabled="disabled" placeholder="如：群像、经营、无恋爱主线" /></div>
          </div>
          <span class="mtp-mini-label">读者预期契约（可选）</span>
          <n-input v-model:value="readerContract" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :disabled="disabled" placeholder="本书承诺的主要阅读回报，以及希望避免的桥段" />
          <n-checkbox v-model:checked="profileConfirmed" :disabled="disabled || !profileFieldsComplete || !selectionLabels.category || !selectionLabels.theme" data-testid="market-profile-confirm">
            我已核对本书频道、主题及五维画像，使用当前内容创作
          </n-checkbox>
          <p v-if="!profileFieldsComplete" class="mtp-note">补全世界观基调、剧情结构、节奏把控、写作风格和特殊要求后，可以完成核对。</p>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { pickLocaleLabel } from '@/domain/taxonomy/types'
import type { AudienceMode, MarketCategory, MarketProfile, MarketSelectionDraft, MarketTheme } from '@/domain/taxonomy/typesV2'
import {
  BUILTIN_CN_MARKET_V2, MARKET_PROFILE_DIMENSIONS, builtinProfileIsReady, createMarketSelectionDraft,
  emptyMarketProfile, filterCategoriesForAudience, filterThemesForCategory, marketSelectionLabels, profileForTheme,
} from '@/domain/taxonomy/cnMarketV2'

withDefaults(defineProps<{ locale?: string; disabled?: boolean }>(), { locale: 'zh-CN', disabled: false })

const selection = defineModel<MarketSelectionDraft>('marketSelection', { default: createMarketSelectionDraft })
const genre = defineModel<string>('genre', { default: '' })
const worldPreset = defineModel<string>('worldPreset', { default: '' })
const storyStructure = defineModel<string>('storyStructure', { default: '' })
const pacingControl = defineModel<string>('pacingControl', { default: '' })
const writingStyle = defineModel<string>('writingStyle', { default: '' })
const specialRequirements = defineModel<string>('specialRequirements', { default: '' })
const searchQuery = ref('')
const showAllCategories = ref(false)
const showAllThemes = ref(false)
const channels = BUILTIN_CN_MARKET_V2.channels
const activeChannel = computed(() => channels.find((item) => item.id === selection.value.audienceMode))
const activeCategory = computed(() => !selection.value.customCategory && BUILTIN_CN_MARKET_V2.categories.find((item) => item.id === selection.value.canonicalCategoryId))
const activeTheme = computed(() => !selection.value.customTheme && BUILTIN_CN_MARKET_V2.themes.find((item) => item.id === selection.value.canonicalThemeId))
const selectionLabels = computed(() => marketSelectionLabels(selection.value))
const filteredCategories = computed(() => filterCategoriesForAudience(selection.value.audienceMode, searchQuery.value))
const filteredThemes = computed(() => filterThemesForCategory(selection.value.canonicalCategoryId, selection.value.audienceMode, searchQuery.value))
const builtinReady = computed(() => !!activeTheme.value && !!selection.value.audienceMode && builtinProfileIsReady(activeTheme.value, selection.value.audienceMode))

const visibleCategories = computed(() => {
  if (showAllCategories.value || searchQuery.value.trim()) return filteredCategories.value
  const featured = new Set(activeChannel.value?.featured_category_ids ?? [])
  const list = filteredCategories.value.filter((item) => featured.has(item.id)).slice(0, 12)
  const selected = filteredCategories.value.find((item) => item.id === selection.value.canonicalCategoryId)
  if (selected && !list.includes(selected)) list.push(selected)
  return list
})
const hiddenCategoryCount = computed(() => Math.max(0, filteredCategories.value.length - visibleCategories.value.length))
const visibleThemes = computed(() => {
  if (showAllThemes.value || searchQuery.value.trim()) return filteredThemes.value
  const list = filteredThemes.value.slice(0, 12)
  const selected = filteredThemes.value.find((item) => item.id === selection.value.canonicalThemeId)
  if (selected && !list.includes(selected)) list.push(selected)
  return list
})
const hiddenThemeCount = computed(() => Math.max(0, filteredThemes.value.length - visibleThemes.value.length))

function updateSelection(patch: Partial<MarketSelectionDraft>) { selection.value = { ...selection.value, ...patch } }
const customCategoryLabel = computed({ get: () => selection.value.customCategoryLabel, set: (value: string) => updateSelection({ customCategoryLabel: value }) })
const customThemeLabel = computed({ get: () => selection.value.customThemeLabel, set: (value: string) => updateSelection({ customThemeLabel: value }) })
const profileConfirmed = computed({ get: () => selection.value.profileConfirmed, set: (value: boolean) => updateSelection({ profileConfirmed: value }) })
const readerContract = computed({ get: () => selection.value.readerContract, set: (value: string) => updateSelection({ readerContract: value }) })
const platform = computed({ get: () => selection.value.platform || null, set: (value: string | null) => updateSelection({ platform: value ?? '' }) })
const tagsText = computed({
  get: () => selection.value.tags.join('、'),
  set: (value: string) => updateSelection({ tags: [...new Set(value.split(/[、,，;；]/).map((item) => item.trim()).filter(Boolean))] }),
})
const platformOptions = BUILTIN_CN_MARKET_V2.platforms.map((item) => ({ label: item.label, value: item.id }))
const currentProfile = computed<MarketProfile>(() => ({
  world_tone: worldPreset.value, story_structure: storyStructure.value, pacing_control: pacingControl.value,
  writing_style: writingStyle.value, special_requirements: specialRequirements.value,
}))
const profileFieldsComplete = computed(() => MARKET_PROFILE_DIMENSIONS.every((key) => currentProfile.value[key].trim()))
const profileCards = computed(() => [
  { key: 'world_tone', title: '世界观基调', note: '时空背景、规则、氛围与主角面对的现实约束。', placeholder: '写清故事发生在哪里、哪些规则改变人物选择，以及整体情绪基调。', model: worldPreset },
  { key: 'story_structure', title: '剧情结构', note: '开篇切入、主线推进、阶段高潮与收束方式。', placeholder: '写清主要矛盾如何出现、发展、兑现与收束。', model: storyStructure },
  { key: 'pacing_control', title: '节奏把控', note: '压力、信息变化、行动回报与阶段结算。', placeholder: '写清小、中、大回报如何安排，以及如何避免连续铺垫。', model: pacingControl },
  { key: 'writing_style', title: '写作风格', note: '叙事视角、场景细节、人物语言与情绪表达。', placeholder: '写清叙事、环境描写和人物对话的具体风格。', model: writingStyle },
  { key: 'special_requirements', title: '特殊要求', note: '主题必须兑现的承诺、限制与需要规避的桥段。', placeholder: '写清本主题的创作要求、人物约束和禁忌。', model: specialRequirements },
])

function applyProfile(profile: Partial<MarketProfile> = {}) {
  const next = { ...emptyMarketProfile(), ...profile }
  worldPreset.value = next.world_tone
  storyStructure.value = next.story_structure
  pacingControl.value = next.pacing_control
  writingStyle.value = next.writing_style
  specialRequirements.value = next.special_requirements
}
function resetChoice(patch: Partial<MarketSelectionDraft>) {
  updateSelection({ canonicalCategoryId: '', canonicalThemeId: '', customCategory: false, customTheme: false,
    customCategoryLabel: '', customThemeLabel: '', profileConfirmed: false, tags: [], readerContract: '', ...patch })
  showAllThemes.value = false
  applyProfile()
}
function pickAudience(mode: AudienceMode) {
  if (mode === selection.value.audienceMode) return
  resetChoice({ audienceMode: mode })
  showAllCategories.value = false
  searchQuery.value = ''
}
function pickCategory(category: MarketCategory) {
  if (!selection.value.customCategory && selection.value.canonicalCategoryId === category.id) return
  resetChoice({ canonicalCategoryId: category.id })
}
function pickCustomCategory() {
  if (selection.value.customCategory) return
  resetChoice({ customCategory: true, customTheme: true })
}
function pickTheme(theme: MarketTheme) {
  if (!selection.value.customTheme && selection.value.canonicalThemeId === theme.id) return
  updateSelection({ canonicalThemeId: theme.id, customTheme: false, customThemeLabel: '', profileConfirmed: false, readerContract: '', tags: [] })
  if (selection.value.audienceMode) applyProfile(profileForTheme(theme, selection.value.audienceMode))
}
function pickCustomTheme() {
  if (selection.value.customTheme) return
  updateSelection({ canonicalThemeId: '', customTheme: true, customThemeLabel: '', profileConfirmed: false, readerContract: '', tags: [] })
  applyProfile(activeCategory.value ? activeCategory.value.default_profile : {})
}

watch(() => selectionLabels.value, (labels) => {
  genre.value = labels.category && labels.theme ? `${labels.category} / ${labels.theme}` : ''
}, { immediate: true })

// A confirmation belongs to the exact choice and text currently displayed.
watch(() => JSON.stringify({
  audience: selection.value.audienceMode, category: selection.value.canonicalCategoryId, theme: selection.value.canonicalThemeId,
  customCategory: selection.value.customCategory, customTheme: selection.value.customTheme,
  categoryLabel: selection.value.customCategoryLabel, themeLabel: selection.value.customThemeLabel,
  platform: selection.value.platform, tags: selection.value.tags, readerContract: selection.value.readerContract,
  profile: currentProfile.value,
}), () => { if (selection.value.profileConfirmed) updateSelection({ profileConfirmed: false }) }, { flush: 'sync' })
</script>

<style scoped>
.mtp { display: flex; flex-direction: column; gap: 10px; }
.mtp--busy { opacity: .72; }
.mtp-section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.mtp-k { font-size: 12px; font-weight: 700; color: var(--app-text-secondary); }
.mtp-hint, .mtp-note { font-size: 12px; color: var(--app-text-muted); line-height: 1.6; }
.mtp-note { margin: 0; }
.mtp-channel-row, .mtp-major-row, .mtp-theme-row { display: flex; flex-wrap: wrap; gap: 8px; }
.mtp-major-row, .mtp-theme-row { max-height: 240px; overflow-y: auto; align-content: flex-start; }
.mtp-major-row { padding: 12px; border-radius: 12px; background: rgba(79, 70, 229, .04); border: 1px solid rgba(79, 70, 229, .12); }
.mtp-detail { display: flex; flex-direction: column; gap: 12px; }
.mtp-classify-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.mtp-classify-strip > div { min-width: 0; padding: 10px 12px; border-radius: 8px; background: var(--app-surface-subtle); border: 1px solid var(--app-border); }
.mtp-classify-strip strong { font-size: 13px; overflow-wrap: anywhere; }
.mtp-mini-label { display: block; margin-bottom: 5px; font-size: 12px; color: var(--app-text-muted); }
.mtp-profile-status { font-size: 12px; }
.mtp-writing-grid, .mtp-extra-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.mtp-profile-card { min-width: 0; padding: 12px; border-radius: 8px; background: rgba(15, 23, 42, .025); border: 1px solid var(--app-border); }
.mtp-profile-card--world { grid-column: 1 / -1; }
.mtp-profile-card > strong { font-size: 13px; color: var(--app-text-primary); }
.mtp-profile-card .mtp-note { margin: 6px 0 9px; }
.mtp-profile-input :deep(textarea) { font-size: 14px; line-height: 1.7; white-space: pre-wrap; }
@media (max-width: 900px) {
  .mtp-classify-strip, .mtp-writing-grid, .mtp-extra-grid { grid-template-columns: 1fr; }
  .mtp-section-head { flex-wrap: wrap; }
}
</style>
