import { afterEach, describe, expect, it } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import MarketTaxonomyPicker from '../src/components/taxonomy/MarketTaxonomyPicker.vue'
import {
  BUILTIN_CN_MARKET_V2, MARKET_PROFILE_DIMENSIONS, buildMarketSelectionPayload, categoriesForAudience,
  createMarketSelectionDraft, emptyMarketProfile, filterCategoriesForAudience, filterThemesForCategory,
  marketSelectionIsComplete, profileForTheme, themesForCategory,
} from '../src/domain/taxonomy/cnMarketV2'
import type { MarketProfile, MarketSelectionDraft } from '../src/domain/taxonomy/typesV2'

const profile: MarketProfile = {
  world_tone: '测试世界与规则', story_structure: '测试开篇与阶段结构', pacing_control: '测试压力与回报节奏',
  writing_style: '测试场景与对白风格', special_requirements: '测试主题承诺与约束',
}
const builtinSelection = (): MarketSelectionDraft => ({
  ...createMarketSelectionDraft(), audienceMode: 'male', canonicalCategoryId: 'major.xuanhuan',
  canonicalThemeId: 'theme.xuanhuan.dongfang', profileConfirmed: true,
})

describe('v2 market selection request contract', () => {
  it('filters categories and themes by the selected audience and keeps dual IDs unique', () => {
    expect(categoriesForAudience('')).toEqual([])
    expect(categoriesForAudience('female').some((item) => item.id === 'major.romance')).toBe(true)
    expect(categoriesForAudience('female').some((item) => item.id === 'major.xuanhuan')).toBe(false)
    const dual = categoriesForAudience('dual').map((item) => item.id)
    expect(new Set(dual).size).toBe(dual.length)
    expect(themesForCategory('major.xuanhuan', 'female')).toEqual([])
    expect(filterCategoriesForAudience('female', '年代').some((item) => item.id === 'major.reality')).toBe(true)
    expect(filterThemesForCategory('major.reality', 'female', '年代').some((item) => item.id === 'theme.reality.period')).toBe(true)
  })

  it('preserves canonical provenance while freezing the exact project override text', () => {
    const selection = builtinSelection()
    selection.tags = ['群像', ' 群像 ', '升级']
    const payload = buildMarketSelectionPayload(selection, { ...profile, world_tone: '  修改后的世界  ' })
    expect(payload.canonical_genre_id).toBe('major.xuanhuan')
    expect(payload.canonical_theme_id).toBe('theme.xuanhuan.dongfang')
    expect(payload.custom_market_profile.source_canonical_theme_id).toBe(payload.canonical_theme_id)
    expect(payload.custom_market_profile.custom_theme.source_canonical_theme_id).toBe(payload.canonical_theme_id)
    expect(payload.custom_market_profile.custom_theme.parent_canonical_category_id).toBe(payload.canonical_genre_id)
    expect(payload.custom_market_profile.custom_theme.profile.world_tone).toBe('修改后的世界')
    expect(payload.custom_market_profile.custom_theme.status).toBe('user_confirmed')
    expect(payload.market_tags).toEqual(['群像', '升级'])
    expect(payload).not.toHaveProperty('profile_snapshot_hash')
  })

  it('supports a canonical category with a custom theme without inventing a canonical theme ID', () => {
    const selection = { ...builtinSelection(), customTheme: true, canonicalThemeId: '', customThemeLabel: '我自己的主题' }
    const payload = buildMarketSelectionPayload(selection, profile)
    expect(payload.canonical_genre_id).toBe('major.xuanhuan')
    expect(payload.canonical_theme_id).toBe('')
    expect(payload.custom_market_profile.custom_theme.label).toBe('我自己的主题')
    expect(payload.custom_market_profile.custom_theme.parent_canonical_category_id).toBe('major.xuanhuan')
    expect(payload.custom_market_profile).not.toHaveProperty('source_canonical_theme_id')
  })

  it('supports a custom category and theme in the chosen audience without modifying the registry', () => {
    const initialCount = BUILTIN_CN_MARKET_V2.categories.length
    const selection = { ...builtinSelection(), customCategory: true, customTheme: true, canonicalCategoryId: '', canonicalThemeId: '',
      customCategoryLabel: ' 自定义大类 ', customThemeLabel: ' 自定义主题 ' }
    const payload = buildMarketSelectionPayload(selection, profile)
    expect(payload.custom_market_profile.custom_category?.label).toBe('自定义大类')
    expect(payload.custom_market_profile.custom_theme.parent_custom_category_id).toBe(payload.custom_market_profile.custom_category?.id)
    expect(payload.custom_market_profile.custom_theme).not.toHaveProperty('parent_canonical_category_id')
    expect(payload.canonical_genre_id).toBe('')
    expect(payload.canonical_theme_id).toBe('')
    expect(payload.audience_mode).toBe('male')
    expect(BUILTIN_CN_MARKET_V2.categories).toHaveLength(initialCount)
  })

  it.each(MARKET_PROFILE_DIMENSIONS)('blocks a missing %s dimension even after confirmation', (key) => {
    expect(() => buildMarketSelectionPayload(builtinSelection(), { ...profile, [key]: '  ' })).toThrow()
  })

  it('blocks unconfirmed profiles and mismatched category/theme or audience choices', () => {
    expect(marketSelectionIsComplete({ ...builtinSelection(), profileConfirmed: false }, profile)).toBe(false)
    expect(marketSelectionIsComplete({ ...builtinSelection(), canonicalCategoryId: 'major.reality' }, profile)).toBe(false)
    expect(marketSelectionIsComplete({ ...builtinSelection(), audienceMode: 'female' }, profile)).toBe(false)
  })
})

const Button = defineComponent({ props: ['disabled'], emits: ['click'], setup(props, { slots, emit }) {
  return () => h('button', { disabled: props.disabled, onClick: () => emit('click') }, slots.default?.())
} })
const Input = defineComponent({ props: ['value', 'disabled'], emits: ['update:value'], setup(props, { emit }) {
  return () => h('textarea', { value: props.value, disabled: props.disabled, onInput: (event: Event) => emit('update:value', (event.target as HTMLTextAreaElement).value) })
} })
const Checkbox = defineComponent({ props: ['checked', 'disabled'], emits: ['update:checked'], setup(props, { emit }) {
  return () => h('input', { type: 'checkbox', checked: props.checked, disabled: props.disabled,
    onChange: (event: Event) => emit('update:checked', (event.target as HTMLInputElement).checked) })
} })
const Box = defineComponent({ setup(_props, { slots }) { return () => h('div', slots.default?.()) } })
const wrappers: VueWrapper[] = []
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

function picker(initialSelection = createMarketSelectionDraft(), initialProfile = emptyMarketProfile()) {
  const state = reactive({ selection: initialSelection, genre: '', profile: initialProfile })
  const Harness = defineComponent({ setup() { return () => h(MarketTaxonomyPicker, {
    marketSelection: state.selection, 'onUpdate:marketSelection': (value) => { state.selection = value },
    genre: state.genre, 'onUpdate:genre': (value) => { state.genre = value },
    worldPreset: state.profile.world_tone, 'onUpdate:worldPreset': (value) => { state.profile.world_tone = value },
    storyStructure: state.profile.story_structure, 'onUpdate:storyStructure': (value) => { state.profile.story_structure = value },
    pacingControl: state.profile.pacing_control, 'onUpdate:pacingControl': (value) => { state.profile.pacing_control = value },
    writingStyle: state.profile.writing_style, 'onUpdate:writingStyle': (value) => { state.profile.writing_style = value },
    specialRequirements: state.profile.special_requirements, 'onUpdate:specialRequirements': (value) => { state.profile.special_requirements = value },
  }) } })
  const wrapper = mount(Harness, { global: { stubs: { NButton: Button, NInput: Input, NCheckbox: Checkbox, NSelect: true, NAlert: Box } } })
  wrappers.push(wrapper)
  return { wrapper, state }
}

describe('actual market picker', () => {
  it('opens audience -> category -> explicit theme and does not silently choose a theme', async () => {
    const { wrapper, state } = picker()
    expect(wrapper.find('[data-testid="market-category-major.xuanhuan"]').exists()).toBe(false)
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.xuanhuan"]').trigger('click')
    expect(state.selection.canonicalThemeId).toBe('')
    expect(state.genre).toBe('')
    await wrapper.get('[data-testid="market-theme-theme.xuanhuan.dongfang"]').trigger('click')
    expect(state.genre).toBe('玄幻 / 东方玄幻')
    expect(state.profile.world_tone).toBe(profileForTheme(BUILTIN_CN_MARKET_V2.themes.find((item) => item.id === 'theme.xuanhuan.dongfang')!, 'male').world_tone)
    expect(state.selection.profileConfirmed).toBe(false)
  })

  it('limits the initial category list and reveals the remaining choices on demand', async () => {
    const { wrapper } = picker()
    await wrapper.get('[data-testid="market-channel-dual"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="market-category-"]').length).toBeLessThanOrEqual(12)
    const expand = wrapper.findAll('button').find((item) => item.text().includes('展开其余'))!
    await expand.trigger('click')
    expect(wrapper.findAll('[data-testid^="market-category-"]').length).toBe(categoriesForAudience('dual').length)
  })

  it('search leaves an already confirmed choice and its text intact', async () => {
    const { wrapper, state } = picker(builtinSelection(), { ...profile })
    await wrapper.get('[data-testid="market-search"]').setValue('没有匹配的词')
    expect(state.selection.canonicalThemeId).toBe('theme.xuanhuan.dongfang')
    expect(state.selection.profileConfirmed).toBe(true)
    expect(state.profile).toEqual(profile)
  })

  it('invalidates the previous confirmation after editing any profile field or changing audience', async () => {
    const { wrapper, state } = picker(builtinSelection(), { ...profile })
    for (const key of MARKET_PROFILE_DIMENSIONS) {
      await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
      expect(state.selection.profileConfirmed).toBe(true)
      await wrapper.get(`[data-testid="market-profile-${key}"]`).setValue(`修改${key}`)
      expect(state.selection.profileConfirmed).toBe(false)
    }
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    await wrapper.get('[data-testid="market-channel-female"]').trigger('click')
    expect(state.selection.profileConfirmed).toBe(false)
    expect(state.selection.canonicalCategoryId).toBe('')
    expect(state.selection.canonicalThemeId).toBe('')
    expect(state.genre).toBe('')
    expect(state.profile).toEqual(emptyMarketProfile())
  })

  it('supports entering both custom names and blocks confirmation until all dimensions are filled', async () => {
    const { wrapper, state } = picker()
    await wrapper.get('[data-testid="market-channel-male"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-category"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-category-label"]').setValue('新大类')
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('新主题')
    expect(state.genre).toBe('新大类 / 新主题')
    expect(wrapper.get('[data-testid="market-profile-confirm"]').attributes('disabled')).toBeDefined()
    for (const key of MARKET_PROFILE_DIMENSIONS) await wrapper.get(`[data-testid="market-profile-${key}"]`).setValue(profile[key])
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    expect(marketSelectionIsComplete(state.selection, state.profile)).toBe(true)
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('更改主题名')
    expect(state.selection.profileConfirmed).toBe(false)
  })

  it('uses neutral category drafts for a custom theme and freezes the author edits', async () => {
    const { wrapper, state } = picker()
    await wrapper.get('[data-testid="market-channel-female"]').trigger('click')
    await wrapper.get('[data-testid="market-search"]').setValue('仙侠')
    await wrapper.get('[data-testid="market-category-major.xianxia"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('无恋爱游仙还愿')
    const category = BUILTIN_CN_MARKET_V2.categories.find((item) => item.id === 'major.xianxia')!
    expect(state.profile).toEqual(category.default_profile)
    expect(category.default_profile_status).toBe('legacy_review_required')
    expect(state.selection.profileConfirmed).toBe(false)
    expect(state.profile.world_tone).not.toContain('男频核心')
    expect(state.profile.special_requirements).toContain('频道不预设性别和恋爱')
    await wrapper.get('[data-testid="market-profile-special_requirements"]').setValue('本书无CP，以还愿与因果闭环，关闭所有恋爱、道侣和飞升大战节点。')
    expect(marketSelectionIsComplete(state.selection, state.profile)).toBe(false)
    await wrapper.get('[data-testid="market-profile-confirm"]').setValue(true)
    const payload = buildMarketSelectionPayload(state.selection, state.profile)
    expect(payload.canonical_theme_id).toBe('')
    expect(payload.custom_market_profile.custom_theme.profile.special_requirements).toContain('关闭所有恋爱、道侣')
    expect(payload.taxonomy_version).toBe(BUILTIN_CN_MARKET_V2.taxonomy_version)
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('另一条还愿主线')
    expect(state.selection.profileConfirmed).toBe(false)
    expect(state.profile.special_requirements).toContain('关闭所有恋爱、道侣')
  })

  it('keeps profiles empty for a custom theme under a category without a default', async () => {
    const { wrapper, state } = picker()
    await wrapper.get('[data-testid="market-channel-female"]').trigger('click')
    await wrapper.get('[data-testid="market-category-major.reality"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme"]').trigger('click')
    await wrapper.get('[data-testid="market-custom-theme-label"]').setValue('我的职场主题')
    expect(state.profile).toEqual(emptyMarketProfile())
    expect(wrapper.get('[data-testid="market-profile-confirm"]').attributes('disabled')).toBeDefined()
    expect(() => buildMarketSelectionPayload(state.selection, state.profile)).toThrow()
  })
})
