// Modified for TaleWeft on 2026-10-22: independent product identity.
export const BRAND = {
  productName: 'TaleWeft',
  chineseName: '叙织',
  displayName: '叙织 · TaleWeft',
  tagline: '编织故事，让灵感成章',
  descriptor: '面向长篇小说的 AI 创作工作台',
} as const

export const BRAND_COPY = {
  short: BRAND.displayName,
  compact: `${BRAND.chineseName} · ${BRAND.tagline}`,
  full: `${BRAND.displayName}｜${BRAND.tagline}`,
} as const
