// Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
import type { Component } from 'vue'

/** 懒加载分区面板（便于 code-splitting） */
export type SettingsSectionLoader = () => Promise<{ default: Component }>

export interface AppSettingsSectionMeta {
  id: string
  label: string
  /** 显示在右侧面板顶部的短说明 */
  description?: string
  /** 越小越靠前 */
  order: number
  component: SettingsSectionLoader
}

const registry: AppSettingsSectionMeta[] = [
  {
    id: 'software-update', label: '软件更新', order: 85,
    description: '手动检查签名更新、保存更改并确认安装',
    component: () => import('@/components/settings/sections/SoftwareUpdateSection.vue'),
  },
  {
    id: 'appearance',
    label: '外观 & 显示',
    description: '配色主题、界面字号与实时预览',
    order: 10,
    component: () => import('@/components/settings/sections/ThemeAppearanceSection.vue'),
  },
  {
    id: 'writing',
    label: '写作偏好',
    description: '每章目标字数、章节计数标签与落盘排版（按书目保存）',
    order: 20,
    component: () => import('@/components/settings/sections/WritingDisplaySection.vue'),
  },
  {
    id: 'autopilot-writing',
    label: '全托管控制',
    description: '审阅闸门与指挥器相位阈值（按书目保存）',
    order: 30,
    component: () => import('@/components/settings/sections/AutopilotWritingPrefsSection.vue'),
  },
  {
    id: 'engine',
    label: '模型引擎',
    description: '多角色端点配置；统一或独立 API Key',
    order: 40,
    component: () => import('@/components/settings/sections/EngineMatrixSection.vue'),
  },
  {
    id: 'credentials',
    label: '凭据管理',
    description: '查看凭据引用、轮换状态与安全清理；导出仅包含不可解密的引用元数据',
    order: 45,
    component: () => import('@/components/settings/sections/CredentialManagementSection.vue'),
  },
  {
    id: 'privacy',
    label: '隐私与诊断',
    description: '诊断内容记录、保留期限与清除',
    order: 50,
    component: () => import('@/components/settings/sections/TracePrivacySection.vue'),
  },
  {
    id: 'about',
    label: '关于叙织',
    description: '产品信息、版本与许可致谢',
    order: 90,
    component: () => import('@/components/settings/sections/AboutBrandSection.vue'),
  },
  {
    id: 'quality-rules',
    label: '文风规则版本',
    description: '内置规则开关、自定义规则、版本恢复与试扫',
    order: 72,
    component: () => import('@/components/settings/sections/QualityRulesSection.vue'),
  },
  {
    id: 'signed-plugins',
    label: '签名插件',
    description: '可信发布者、插件签名、权限与安装管理',
    order: 74,
    component: () => import('@/components/settings/sections/SignedPluginsSection.vue'),
  },
  {
    id: 'database-backup',
    label: '备份与恢复',
    description: '数据库在线备份、校验与独立恢复演练',
    order: 60,
    component: () => import('@/components/settings/sections/DatabaseBackupSection.vue'),
  },
  {
    id: 'security-audit',
    label: '访问审计',
    description: 'API 修改与拒绝访问记录',
    order: 62,
    component: () => import('@/components/settings/sections/SecurityAuditSection.vue'),
  },
  {
    id: 'manuscript-transfer',
    label: '稿件导入导出',
    description: 'JSON稿件校验、影响预览与导入新作品',
    order: 65,
    component: () => import('@/components/settings/sections/ManuscriptTransferSection.vue'),
  },
]

/**
 * 注册或覆盖设置分区（插件 / 后续功能包可调用此方法扩充界面）
 */
export function registerAppSettingsSection(meta: AppSettingsSectionMeta): void {
  const i = registry.findIndex((s) => s.id === meta.id)
  if (i >= 0) registry[i] = meta
  else registry.push(meta)
  registry.sort((a, b) => a.order - b.order)
}

export function getAppSettingsSections(): AppSettingsSectionMeta[] {
  return registry.slice().sort((a, b) => a.order - b.order)
}

export function isRegisteredSettingsSectionId(id: string): boolean {
  return registry.some((s) => s.id === id)
}

export const DEFAULT_SETTINGS_SECTION_ID = 'appearance' as const
