/**
 * 将共享 YAML 解析为 JSON，供 Vite/TypeScript 静态 import。
 * YAML 是权威数据源；JSON 仅为前端打包用的生成物。
 * 脚本放在 frontend/ 下以便解析 devDependency `yaml`。
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'yaml'

const checkOnly = process.argv.includes('--check')
const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '..', '..')
const targets = [
  {
    src: resolve(repoRoot, 'shared/taxonomy/builtin_cn_v1.yaml'),
    out: resolve(repoRoot, 'frontend/src/domain/taxonomy/builtin_cn_v1.bundle.json'),
    label: 'taxonomy',
  },
  {
    src: resolve(repoRoot, 'shared/taxonomy/builtin_cn_v2.yaml'),
    out: resolve(repoRoot, 'frontend/src/domain/taxonomy/builtin_cn_v2.bundle.json'),
    label: 'taxonomy-v2',
  },
  {
    src: resolve(repoRoot, 'shared/taxonomy/worldbuilding_contract_cn_v1.yaml'),
    out: resolve(repoRoot, 'frontend/src/domain/worldbuilding/contract.bundle.json'),
    label: 'worldbuilding-contract',
  },
]

const bannedTaxonomyPhrases = [
  '独有卖点',
  '第一次利用',
  '规则解决眼前问题',
  '压力出现 → 主角试探',
  '专属场景完成',
  '服务{{topic}}受众',
  '只写成背景皮肤',
  '标志性元素',
]

function validateTaxonomy(doc, target) {
  if (target.label === 'taxonomy') {
    const serialized = JSON.stringify(doc)
    const hits = bannedTaxonomyPhrases.filter((phrase) => serialized.includes(phrase))
    if (hits.length > 0) {
      throw new Error(`[sync:${target.label}] banned template phrases found: ${hits.join(', ')}`)
    }
    return
  }
  if (target.label !== 'taxonomy-v2') return

  if (doc?.schema_kind !== 'plotpilot.cn_market_taxonomy' || doc?.schema_version !== 2) {
    throw new Error(`[sync:${target.label}] unsupported taxonomy schema`)
  }
  const channels = new Map((Array.isArray(doc.channels) ? doc.channels : []).map((item) => [item.id, item]))
  const categories = new Map((Array.isArray(doc.categories) ? doc.categories : []).map((item) => [item.id, item]))
  const themes = new Map((Array.isArray(doc.themes) ? doc.themes : []).map((item) => [item.id, item]))
  if (channels.size !== 3 || !['male', 'female', 'dual'].every((id) => channels.has(id))) {
    throw new Error(`[sync:${target.label}] channels must define male, female, and dual`)
  }
  if (categories.size !== doc.categories.length || themes.size !== doc.themes.length) {
    throw new Error(`[sync:${target.label}] canonical ids must be unique`)
  }
  for (const category of categories.values()) {
    if (!Array.isArray(category.theme_ids) || category.theme_ids.some((id) => !themes.has(id))) {
      throw new Error(`[sync:${target.label}] category ${category.id} has unknown theme ids`)
    }
    for (const id of category.theme_ids) {
      if (themes.get(id).category_id !== category.id) {
        throw new Error(`[sync:${target.label}] theme ${id} parent does not match ${category.id}`)
      }
    }
  }
  const dimensions = doc.profile_dimensions
  if (!Array.isArray(dimensions) || dimensions.length !== 5 || new Set(dimensions).size !== 5) {
    throw new Error(`[sync:${target.label}] profile_dimensions must define five unique dimensions`)
  }
  for (const theme of themes.values()) {
    if (!categories.has(theme.category_id) || !categories.get(theme.category_id).theme_ids.includes(theme.id)) {
      throw new Error(`[sync:${target.label}] theme ${theme.id} references an unknown or mismatched parent`)
    }
    if (theme.status === 'ready') {
      if (!theme.profile_ref || !theme.profile_version || !theme.profile_source?.file || !theme.profile_source?.type) {
        throw new Error(`[sync:${target.label}] ready theme ${theme.id} is missing profile metadata`)
      }
      if (!theme.profile || dimensions.some((key) => typeof theme.profile[key] !== 'string' || !theme.profile[key].trim())) {
        throw new Error(`[sync:${target.label}] ready theme ${theme.id} must have all five profile dimensions`)
      }
    }
    const overlays = theme.audience_profile_overlays
    if (!overlays || Object.keys(overlays).length !== theme.audience_modes?.length ||
        theme.audience_modes.some((mode) => !overlays[mode])) {
      throw new Error(`[sync:${target.label}] theme ${theme.id} must define overlays for its audience modes`)
    }
  }
  for (const channel of channels.values()) {
    if (!Array.isArray(channel.category_ids) || channel.category_ids.some((id) => !categories.has(id))) {
      throw new Error(`[sync:${target.label}] channel ${channel.id} references unknown categories`)
    }
    if (!Array.isArray(channel.featured_category_ids) || channel.featured_category_ids.some((id) => !channel.category_ids.includes(id))) {
      throw new Error(`[sync:${target.label}] channel ${channel.id} has invalid featured categories`)
    }
  }
  for (const platform of doc.platforms ?? []) {
    for (const mapping of platform.mappings ?? []) {
      if (mapping.canonical_category_id != null && !categories.has(mapping.canonical_category_id)) {
        throw new Error(`[sync:${target.label}] platform mapping references unknown category ${mapping.canonical_category_id}`)
      }
      if (mapping.canonical_theme_id != null) {
        const theme = themes.get(mapping.canonical_theme_id)
        if (!theme || theme.category_id !== mapping.canonical_category_id) {
          throw new Error(`[sync:${target.label}] platform mapping references unknown or mismatched theme ${mapping.canonical_theme_id}`)
        }
      }
      if ((mapping.candidate_category_ids ?? []).some((id) => !categories.has(id))) {
        throw new Error(`[sync:${target.label}] platform mapping references an unknown candidate category`)
      }
    }
  }
}

for (const target of targets) {
  const doc = parse(readFileSync(target.src, 'utf8'))
  validateTaxonomy(doc, target)
  const next = `${JSON.stringify(doc, null, 2)}\n`
  if (checkOnly) {
    const current = readFileSync(target.out, 'utf8')
    if (current !== next) {
      console.error(`[sync:${target.label}] stale bundle`, target.out)
      process.exitCode = 1
    } else {
      console.log(`[sync:${target.label}] up to date`, target.out)
    }
  } else {
    writeFileSync(target.out, next, 'utf8')
    console.log(`[sync:${target.label}] wrote`, target.out)
  }
}
