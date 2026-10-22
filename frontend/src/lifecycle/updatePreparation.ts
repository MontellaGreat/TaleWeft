import { onScopeDispose, readonly, ref } from 'vue'

type SaveBarrier = () => Promise<boolean>
const barriers = new Set<SaveBarrier>()
const preparing = ref(false)
export const updatePreparing = readonly(preparing)

export function registerUpdateSaveBarrier(barrier: SaveBarrier): () => void {
  barriers.add(barrier)
  return () => barriers.delete(barrier)
}

export function useUpdateSaveBarrier(barrier: SaveBarrier): void {
  onScopeDispose(registerUpdateSaveBarrier(barrier))
}

/** Save failure is a hard stop: install is never invoked. */
export async function prepareAndInstallUpdate(install: () => Promise<unknown>): Promise<void> {
  if (preparing.value) throw new Error('更新准备正在进行')
  preparing.value = true
  const blockEditing = (event: Event) => { event.preventDefault(); event.stopImmediatePropagation() }
  const events = ['beforeinput', 'keydown', 'paste', 'drop'] as const
  events.forEach(name => document.addEventListener(name, blockEditing, true))
  try {
    for (const save of [...barriers]) {
      if (!await save()) throw new Error('仍有更改未成功保存，已取消安装更新')
    }
    await install()
  } finally {
    events.forEach(name => document.removeEventListener(name, blockEditing, true))
    preparing.value = false
  }
}
