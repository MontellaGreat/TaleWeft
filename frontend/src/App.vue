<script setup lang="ts">
import { computed } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, zhCN, dateZhCN, darkTheme } from 'naive-ui'
import AppSettingsModal from './components/settings/AppSettingsModal.vue'
import AIInvocationReviewPanel from './components/ai-invocation/AIInvocationReviewPanel.vue'
import { featureFlags } from './config/features'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useThemeStore } from './stores/themeStore'
import { useFontSizeStore, scaledUiPx, type FontSizePreset } from './stores/fontSizeStore'
import { NAIVE_DENSITY_BASE } from './design/layoutDensity'

const themeStore = useThemeStore()
const fontSizeStore = useFontSizeStore()

const naiveTheme = computed(() =>
  themeStore.isDark ? darkTheme : undefined
)

// ─── 静态调色板（不随主题变化，提升出来避免 computed 重建字符串）─────────────
const LIGHT_PALETTE = {
  primary:        '#215f56',
  primaryHover:   '#34796c',
  primaryPressed: '#194b44',
  primarySuppl:   '#83c9b3',
  text1:          '#24332f',
  text2:          '#596b64',
  text3:          '#63776d',
  border:         'rgba(15, 23, 42, 0.09)',
  divider:        'rgba(15, 23, 42, 0.06)',
  surface:        '#ffffff',
  tableStriped:   '#f4f7f5',
  tableHover:     '#f4f7f5',
  inputBg:        '#ffffff',
  drawerBg:       '#f1f4f2',
  selectBorder:   '#215f56',
} as const

const DARK_PALETTE = {
  primary:        '#83c9b3',
  primaryHover:   '#a2ddc9',
  primaryPressed: '#34796c',
  primarySuppl:   '#c3eadc',
  text1:          '#e4ece7',
  text2:          '#b3c2ba',
  text3:          '#63776d',
  border:         'rgba(148, 163, 184, 0.12)',
  divider:        'rgba(148, 163, 184, 0.08)',
  surface:        '#252f2b',
  tableStriped:   '#202a25',
  tableHover:     '#303e36',
  inputBg:        '#202a25',
  drawerBg:       '#1c2421',
  selectBorder:   '#83c9b3',
} as const

const ANCHOR_PALETTE = {
  primary:        '#c9a227',
  primaryHover:   '#ddb930',
  primaryPressed: '#a88a1f',
  primarySuppl:   '#e8c84a',
  text1:          '#f0ead6',
  text2:          '#c4b99a',
  text3:          '#8a8070',
  border:         'rgba(201, 162, 39, 0.14)',
  divider:        'rgba(201, 162, 39, 0.06)',
  surface:        '#111620',
  tableStriped:   '#0d1018',
  tableHover:     '#181f2e',
  inputBg:        '#0d1018',
  drawerBg:       '#0a0c10',
  selectBorder:   '#c9a227',
} as const

/** Naive UI 形体：随字体档位缩放，基准见 design/layoutDensity */
function naiveShapeOverrides(fz: FontSizePreset): GlobalThemeOverrides {
  const r = scaledUiPx(NAIVE_DENSITY_BASE.borderRadius, fz)
  const rs = scaledUiPx(NAIVE_DENSITY_BASE.borderRadiusSmall, fz)
  const cr = scaledUiPx(NAIVE_DENSITY_BASE.cardBorderRadius, fz)
  const sb = scaledUiPx(NAIVE_DENSITY_BASE.scrollbarWidth, fz)
  return {
    common: {
      borderRadius: r,
      borderRadiusSmall: rs,
      fontSize: scaledUiPx(NAIVE_DENSITY_BASE.fontSize, fz),
      fontSizeMedium: scaledUiPx(NAIVE_DENSITY_BASE.fontSizeMedium, fz),
      lineHeight: NAIVE_DENSITY_BASE.lineHeight,
      heightMedium: scaledUiPx(NAIVE_DENSITY_BASE.heightMedium, fz),
    },
    Card: {
      borderRadius: cr,
      paddingMedium: scaledUiPx(NAIVE_DENSITY_BASE.cardPaddingMedium, fz),
    },
    Button: { borderRadiusMedium: r },
    Input: { borderRadius: r },
    Scrollbar: { width: sb, height: sb, borderRadius: scaledUiPx(3, fz) },
    DataTable: { borderRadius: r, thFontWeight: '600' },
    Tag: { borderRadius: scaledUiPx(5, fz) },
    Progress: {
      railBorderRadius: scaledUiPx(3, fz),
      fillBorderRadius: scaledUiPx(3, fz),
    },
    Drawer: { bodyPadding: '0' },
    Alert: { border: 'none' },
  }
}

// ─── 颜色 + 字号档位是动态的，量少性能好 ─────────────────────────────────────
const themeOverrides = computed<GlobalThemeOverrides>(() => {
  const p = themeStore.isAnchor ? ANCHOR_PALETTE
          : themeStore.isDark   ? DARK_PALETTE
          :                       LIGHT_PALETTE
  const fz = fontSizeStore.preset

  const shape = naiveShapeOverrides(fz)
  return {
    ...shape,
    common: {
      ...shape.common,
      primaryColor:        p.primary,
      primaryColorHover:   p.primaryHover,
      primaryColorPressed: p.primaryPressed,
      primaryColorSuppl:   p.primarySuppl,
      bodyColor:           p.drawerBg,
      textColor1:          p.text1,
      textColor2:          p.text2,
      textColor3:          p.text3,
      borderColor:         p.border,
      dividerColor:        p.divider,
      cardColor:           p.surface,
      modalColor:          p.surface,
      popoverColor:        p.surface,
      tableColor:          p.surface,
      tableColorStriped:   p.tableStriped,
      tableColorHover:     p.tableHover,
      tableHeaderColor:    p.surface,
    },
    Select: {
      peers: {
        InternalSelection: {
          color:       p.inputBg,
          borderActive: p.selectBorder,
          borderFocus:  p.selectBorder,
        },
      },
    },
    Drawer: { ...shape.Drawer, color: p.drawerBg },
    Tabs: {
      tabTextColorActiveLine: p.primary,
      tabTextColorHoverLine:  p.text2,
      barColor:               p.primary,
    },
    Switch: { railColorActive: p.primary },
    Alert:  { ...shape.Alert, color: p.surface },
    Form:   { labelTextColorTop: p.text2 },
  }
})
</script>

<template>
  <n-config-provider
    :locale="zhCN"
    :date-locale="dateZhCN"
    :theme="naiveTheme"
    :theme-overrides="themeOverrides"
  >
    <n-message-provider>
      <n-dialog-provider>
        <router-view v-slot="{ Component }">
          <transition name="app-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
        <AIInvocationReviewPanel v-if="featureFlags.aiInvocationDebug" />
        <AppSettingsModal />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
.app-fade-enter-active,
.app-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.app-fade-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
.app-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
