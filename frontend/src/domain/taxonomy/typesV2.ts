import type { LocalizedLabels } from './types'

export type AudienceMode = 'male' | 'female' | 'dual'

export interface MarketProfile {
  world_tone: string
  story_structure: string
  pacing_control: string
  writing_style: string
  special_requirements: string
}

export interface MarketChannel {
  id: AudienceMode
  labels: LocalizedLabels
  description: string
  category_ids: string[]
  featured_category_ids: string[]
}

export interface MarketCategory {
  id: string
  labels: LocalizedLabels
  description: string
  audience_modes: AudienceMode[]
  theme_ids: string[]
  default_profile?: MarketProfile
  default_profile_status: string
}

export interface MarketTheme {
  id: string
  category_id: string
  labels: LocalizedLabels
  description: string
  audience_modes: AudienceMode[]
  profile: MarketProfile
  profile_version: string
  status: string
  audience_profile_overlays: Partial<Record<AudienceMode, { status: string; profile: Partial<MarketProfile> | null }>>
}

export interface MarketTaxonomyBundleV2 {
  schema_kind: 'plotpilot.cn_market_taxonomy'
  schema_version: 2
  taxonomy_version: string
  channels: MarketChannel[]
  categories: MarketCategory[]
  themes: MarketTheme[]
  platforms: { id: string; label: string }[]
}

/** The current project's selection; custom entries never modify the builtin registry. */
export interface MarketSelectionDraft {
  audienceMode: AudienceMode | ''
  canonicalCategoryId: string
  canonicalThemeId: string
  customCategory: boolean
  customTheme: boolean
  customCategoryLabel: string
  customThemeLabel: string
  entryKey: string
  platform: string
  tags: string[]
  readerContract: string
  profileConfirmed: boolean
}

export interface ProjectMarketProfile {
  scope: 'project'
  audience_mode: AudienceMode
  profile_review_status: 'user_confirmed'
  source_canonical_theme_id?: string
  custom_category?: { id: string; label: string; profile: MarketProfile }
  custom_theme: {
    id: string
    label: string
    parent_canonical_category_id?: string
    parent_custom_category_id?: string
    source_canonical_theme_id?: string
    source_profile_version?: string
    status: 'user_confirmed'
    profile: MarketProfile
  }
}

export interface MarketSelectionPayload {
  audience_mode: AudienceMode
  canonical_genre_id: string
  canonical_theme_id: string
  market_platform: string
  market_category_path: string[]
  market_tags: string[]
  taxonomy_version: string
  profile_version: string
  reader_contract: string
  custom_market_profile: ProjectMarketProfile
}
