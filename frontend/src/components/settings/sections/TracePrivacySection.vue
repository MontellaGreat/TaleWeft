<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <n-space vertical :size="18">
    <n-alert type="info" :show-icon="true">
      默认记录调用阶段、耗时、用量、内容哈希与经脱敏的短预览。开启全文记录后，提示词、正文与输出全文会保存在本机诊断记录中。
    </n-alert>
    <n-form label-placement="left" label-width="140">
      <n-form-item label="设置范围"><n-select v-model:value="novelId" :options="novels" :disabled="busy" @update:value="loadSettings" /></n-form-item>
      <n-form-item label="保留调用诊断"><n-switch v-model:value="settings.enabled" /></n-form-item>
      <n-form-item label="记录提示词与正文全文"><n-switch v-model:value="settings.capture_fulltext" :disabled="!settings.enabled" /></n-form-item>
      <n-form-item label="保留天数"><n-input-number v-model:value="settings.retention_days" :min="1" :max="365" /></n-form-item>
      <n-form-item label="最多调用记录"><n-input-number v-model:value="settings.max_spans" :min="100" :max="100000" :step="100" /></n-form-item>
    </n-form>
    <n-space>
      <n-button type="primary" :loading="busy" @click="save">保存设置</n-button>
      <n-button v-if="novelId" :disabled="busy" @click="reset">使用默认设置</n-button>
      <n-button :disabled="busy" @click="exportRecords">导出调用诊断</n-button>
      <n-popconfirm @positive-click="clearRecords">
        <template #trigger><n-button type="error" :disabled="busy">清除调用诊断</n-button></template>
        清除{{ novelId ? '此小说' : '本机所有小说' }}的调用诊断？小说正文与创作数据会保留。
      </n-popconfirm>
    </n-space>
  </n-space>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NInputNumber, NPopconfirm, NSelect, NSpace, NSwitch, useMessage } from 'naive-ui'
import { settingsApi, type TracePrivacySettings } from '@/api/settings'
import { novelApi } from '@/api/novel'
const settings = reactive<TracePrivacySettings>({ enabled: true, capture_fulltext: false, retention_days: 14, max_spans: 10000 })
const novelId = ref('')
const novels = ref([{ label: '默认设置（小说可独立覆盖）', value: '' }])
const busy = ref(false)
const message = useMessage()
let generation = 0
async function loadSettings() {
  const current = ++generation
  busy.value = true
  try {
    const data = await settingsApi.getTracePrivacy(novelId.value || undefined)
    if (current === generation) Object.assign(settings, data)
  }
  catch { message.error('无法读取诊断设置') }
  finally { if (current === generation) busy.value = false }
}
onMounted(async () => {
  await loadSettings()
  try { novels.value.push(...(await novelApi.listNovels()).map(item => ({ label: item.title, value: item.id }))) }
  catch { message.error('无法读取小说列表') }
})
async function save() {
  busy.value = true
  try { Object.assign(settings, await settingsApi.saveTracePrivacy({ ...settings }, novelId.value || undefined)); message.success('诊断设置已保存') }
  catch { message.error('诊断设置保存失败') }
  finally { busy.value = false }
}
async function clearRecords() {
  busy.value = true
  try { await settingsApi.clearTraceRecords(novelId.value || undefined); message.success('调用诊断已清除') }
  catch { message.error('诊断清除失败') }
  finally { busy.value = false }
}
async function reset() {
  busy.value = true
  try { Object.assign(settings, await settingsApi.resetTracePrivacy(novelId.value)); message.success('已使用默认设置') }
  catch { message.error('诊断设置恢复失败') }
  finally { busy.value = false }
}
async function exportRecords() {
  busy.value = true
  try {
    const data = await settingsApi.exportTraceRecords(novelId.value || undefined)
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'taleweft-ai-diagnostics.json'
    link.click()
    URL.revokeObjectURL(url)
  } catch { message.error('诊断导出失败') }
  finally { busy.value = false }
}
</script>
