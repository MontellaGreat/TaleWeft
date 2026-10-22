<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <n-space vertical :size="16">
    <n-alert type="info" :show-icon="true">
      这里仅显示凭据引用和脱敏元数据。API Key 永远不会出现在列表、导出文件或删除错误中；轮换请在“模型引擎”中保存新的 API Key。
    </n-alert>
    <n-space>
      <n-button :loading="busy" @click="reload">刷新状态</n-button>
      <n-button :loading="busy" @click="exportMetadata">导出引用元数据</n-button>
    </n-space>
    <n-data-table
      :columns="columns"
      :data="credentials"
      :loading="busy"
      :pagination="false"
      :bordered="false"
      :single-line="false"
    />
    <n-alert v-if="error" type="error" :show-icon="true">{{ error }}
    </n-alert>
  </n-space>
</template>

<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NAlert, NButton, NDataTable, NPopconfirm, NSpace, NTag, useMessage, type DataTableColumns } from 'naive-ui'
import { settingsApi, type CredentialReference } from '@/api/settings'

const credentials = ref<CredentialReference[]>([])
const busy = ref(false)
const error = ref('')
const message = useMessage()

const statusLabel: Record<CredentialReference['status'], string> = {
  active: '当前引用中', orphaned: '未被引用，可清理', missing: '文件缺失',
}
const statusType: Record<CredentialReference['status'], 'success' | 'warning' | 'error'> = {
  active: 'success', orphaned: 'warning', missing: 'error',
}

const columns: DataTableColumns<CredentialReference> = [
  { title: '凭据引用', key: 'credential_ref', ellipsis: { tooltip: true }, minWidth: 260 },
  { title: '状态', key: 'status', width: 130, render: row => h(NTag, { type: statusType[row.status] }, { default: () => statusLabel[row.status] }) },
  { title: '引用位置', key: 'owners', minWidth: 180, render: row => row.owners.length ? row.owners.map(owner => `${owner.table}.${owner.field}`).join('、') : '无' },
  { title: '密文 SHA-256', key: 'ciphertext_sha256', minWidth: 220, ellipsis: { tooltip: true }, render: row => row.ciphertext_sha256 || '不可用' },
  { title: '操作', key: 'actions', width: 130, render: row => h(NPopconfirm, { onPositiveClick: () => remove(row) }, { trigger: () => h(NButton, { size: 'small', type: 'error', disabled: row.status !== 'orphaned' }, { default: () => '安全删除' }), default: () => '删除此未引用凭据？备份恢复仍可能需要它。' }) },
]

async function reload() {
  busy.value = true; error.value = ''
  try { credentials.value = (await settingsApi.listCredentials()).credentials }
  catch { error.value = '无法读取凭据引用状态' }
  finally { busy.value = false }
}
async function remove(row: CredentialReference) {
  busy.value = true; error.value = ''
  try { await settingsApi.deleteCredential(row.credential_ref); message.success('未引用凭据已安全删除'); await reload() }
  catch (cause: any) { error.value = cause?.response?.data?.detail?.message || '删除失败：凭据仍被引用或文件不可用' }
  finally { busy.value = false }
}
async function exportMetadata() {
  busy.value = true; error.value = ''
  try {
    const data = await settingsApi.exportCredentials()
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = 'taleweft-credential-refs.json'; link.click(); URL.revokeObjectURL(url)
    message.success('已导出脱敏凭据引用')
  } catch { error.value = '凭据引用导出失败' }
  finally { busy.value = false }
}
onMounted(reload)
</script>
