import { invoke } from '@tauri-apps/api/core'

export interface SoftwareUpdateStatus {
  available: boolean
  stage: 'unavailable' | 'idle' | 'available' | 'verified'
  version: string | null
  notes: string | null
  message: string
}

export const softwareUpdateApi = {
  async status(): Promise<SoftwareUpdateStatus> {
    if (!(window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
      return { available: false, stage: 'unavailable', version: null, notes: null,
        message: '浏览器模式不提供软件安装更新，请使用桌面版。' }
    }
    return invoke('updater_status')
  },
  check: () => invoke<SoftwareUpdateStatus>('updater_check'),
  download: () => invoke<SoftwareUpdateStatus>('updater_download'),
  install: () => invoke<void>('updater_install', { authorConfirmed: true }),
}
