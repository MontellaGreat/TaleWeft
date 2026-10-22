import raw from './builtin_cn_v2.bundle.json'
import { pickLocaleLabel } from './types'
import type {
  AudienceMode,
  MarketCategory,
  MarketProfile,
  MarketSelectionDraft,
  MarketSelectionPayload,
  MarketTaxonomyBundleV2,
  MarketTheme,
} from './typesV2'

/** Generated from the shared YAML by npm run sync:taxonomy. */
export const BUILTIN_CN_MARKET_V2 = raw as MarketTaxonomyBundleV2
export const MARKET_PROFILE_DIMENSIONS = ['world_tone', 'story_structure', 'pacing_control', 'writing_style', 'special_requirements'] as const

export function emptyMarketProfile(): MarketProfile {
  return { world_tone: '', story_structure: '', pacing_control: '', writing_style: '', special_requirements: '' }
}

export function createMarketSelectionDraft(): MarketSelectionDraft {
  return {
    audienceMode: '', canonicalCategoryId: '', canonicalThemeId: '',
    customCategory: false, customTheme: false, customCategoryLabel: '', customThemeLabel: '',
    entryKey: `entry-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    platform: '', tags: [], readerContract: '', profileConfirmed: false,
  }
}

export function categoriesForAudience(mode: AudienceMode | ''): MarketCategory[] {
  const channel = BUILTIN_CN_MARKET_V2.channels.find((item) => item.id === mode)
  if (!channel) return []
  const ids = new Set(channel.category_ids)
  return BUILTIN_CN_MARKET_V2.categories.filter((item) => ids.has(item.id) && item.audience_modes.includes(channel.id))
}

export function themesForCategory(categoryId: string, mode: AudienceMode | ''): MarketTheme[] {
  if (!mode) return []
  const category = categoriesForAudience(mode).find((item) => item.id === categoryId)
  if (!category) return []
  const ids = new Set(category.theme_ids)
  return BUILTIN_CN_MARKET_V2.themes.filter((item) => ids.has(item.id) && item.category_id === categoryId && item.audience_modes.includes(mode))
}

function searchText(item: { id: string; labels: Record<string, string>; description: string }): string {
  return `${item.id} ${Object.values(item.labels).join(' ')} ${item.description}`.toLowerCase()
}

/** Search narrows visible choices and never changes the active selection. */
export function filterCategoriesForAudience(mode: AudienceMode | '', query: string): MarketCategory[] {
  const q = query.trim().toLowerCase()
  return categoriesForAudience(mode).filter((category) => !q || searchText(category).includes(q) || themesForCategory(category.id, mode).some((theme) => searchText(theme).includes(q)))
}

export function filterThemesForCategory(categoryId: string, mode: AudienceMode | '', query: string): MarketTheme[] {
  const q = query.trim().toLowerCase()
  const category = categoriesForAudience(mode).find((item) => item.id === categoryId)
  const categoryMatches = category && searchText(category).includes(q)
  return themesForCategory(categoryId, mode).filter((theme) => !q || categoryMatches || searchText(theme).includes(q))
}

/** Draft text is displayed for review; only reviewed overlays may overwrite it. */
export function profileForTheme(theme: MarketTheme, mode: AudienceMode): MarketProfile {
  const overlay = theme.audience_profile_overlays[mode]
  return { ...emptyMarketProfile(), ...theme.profile, ...(overlay?.status === 'defined' ? overlay.profile : {}) }
}

export function builtinProfileIsReady(theme: MarketTheme, mode: AudienceMode): boolean {
  const overlayStatus = theme.audience_profile_overlays[mode]?.status
  return theme.status === 'ready' && (overlayStatus === 'defined' || overlayStatus === 'no_difference')
}

export function marketSelectionLabels(selection: MarketSelectionDraft): { category: string; theme: string } {
  const category = selection.customCategory
    ? selection.customCategoryLabel.trim()
    : pickLocaleLabel(BUILTIN_CN_MARKET_V2.categories.find((item) => item.id === selection.canonicalCategoryId) ?? { id: '', labels: {} })
  const theme = selection.customTheme
    ? selection.customThemeLabel.trim()
    : pickLocaleLabel(BUILTIN_CN_MARKET_V2.themes.find((item) => item.id === selection.canonicalThemeId) ?? { id: '', labels: {} })
  return { category, theme }
}

export function marketSelectionIsComplete(selection: MarketSelectionDraft, profile: MarketProfile): boolean {
  if (!selection.audienceMode || !selection.profileConfirmed || MARKET_PROFILE_DIMENSIONS.some((key) => !profile[key].trim())) return false
  if (selection.customCategory) return selection.customTheme && !!selection.customCategoryLabel.trim() && !!selection.customThemeLabel.trim()
  const category = categoriesForAudience(selection.audienceMode).find((item) => item.id === selection.canonicalCategoryId)
  if (!category) return false
  if (selection.customTheme) return !!selection.customThemeLabel.trim()
  return themesForCategory(category.id, selection.audienceMode).some((item) => item.id === selection.canonicalThemeId)
}

/** Freeze the final user-reviewed text as a project override and keep canonical source IDs. */
export function buildMarketSelectionPayload(selection: MarketSelectionDraft, inputProfile: MarketProfile): MarketSelectionPayload {
  if (!marketSelectionIsComplete(selection, inputProfile)) throw new Error('请选择频道、大类和主题，并核对完整的五维画像')
  const mode = selection.audienceMode as AudienceMode
  const profile = Object.fromEntries(MARKET_PROFILE_DIMENSIONS.map((key) => [key, inputProfile[key].trim()])) as unknown as MarketProfile
  const categoryId = selection.customCategory ? `custom.category.${selection.entryKey}` : selection.canonicalCategoryId
  const themeId = `custom.theme.${selection.entryKey}`
  const sourceTheme = !selection.customTheme ? BUILTIN_CN_MARKET_V2.themes.find((item) => item.id === selection.canonicalThemeId) : undefined
  const labels = marketSelectionLabels(selection)
  return {
    audience_mode: mode,
    canonical_genre_id: selection.customCategory ? '' : selection.canonicalCategoryId,
    canonical_theme_id: sourceTheme?.id ?? '',
    market_platform: selection.platform,
    market_category_path: [mode, categoryId, sourceTheme?.id ?? themeId],
    market_tags: [...new Set(selection.tags.map((tag) => tag.trim()).filter(Boolean))],
    taxonomy_version: BUILTIN_CN_MARKET_V2.taxonomy_version,
    profile_version: 'project-user-confirmed-v1',
    reader_contract: selection.readerContract.trim(),
    custom_market_profile: {
      scope: 'project',
      audience_mode: mode,
      profile_review_status: 'user_confirmed',
      ...(sourceTheme ? { source_canonical_theme_id: sourceTheme.id } : {}),
      ...(selection.customCategory ? { custom_category: { id: categoryId, label: labels.category, profile: { ...profile } } } : {}),
      custom_theme: {
        id: themeId,
        label: labels.theme,
        ...(selection.customCategory ? { parent_custom_category_id: categoryId } : { parent_canonical_category_id: categoryId }),
        ...(sourceTheme ? { source_canonical_theme_id: sourceTheme.id, source_profile_version: sourceTheme.profile_version } : {}),
        status: 'user_confirmed',
        profile,
      },
    },
  }
}
