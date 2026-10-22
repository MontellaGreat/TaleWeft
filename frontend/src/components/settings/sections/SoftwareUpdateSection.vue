<template>
  <section class="software-update">
    <p>{{ status?.message || '正在读取本地更新配置…' }}</p>
    <p v-if="status?.version">可用版本：{{ status.version }}</p>
    <pre v-if="status?.notes">{{ status.notes }}</pre>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-button :disabled="!status?.available || busy" @click="perform('check')">检查更新</n-button>
    <n-button v-if="status?.stage === 'available'" :disabled="busy" @click="perform('download')">下载并验证签名</n-button>
    <template v-if="status?.stage === 'verified'">
      <n-checkbox v-model:checked="confirmed" :disabled="busy">我已保存其它设置更改，同意停止后台任务并安装更新</n-checkbox>
      <n-button type="primary" :disabled="!confirmed || busy" @click="install">保存并安装更新</n-button>
    </template>
    <p>仅在点击检查或下载时联网。安装前会保存当前正文、世界观和节点配置，并等待后台停止与数据库落盘确认。</p>
    <Teleport to="body">
      <div v-if="updatePreparing" class="update-overlay" role="alert" aria-live="assertive">正在保存更改并准备更新，请等待软件退出。</div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import { softwareUpdateApi, type SoftwareUpdateStatus } from '@/api/softwareUpdate'
import { prepareAndInstallUpdate, updatePreparing } from '@/lifecycle/updatePreparation'
const status = ref<SoftwareUpdateStatus | null>(null)
const error = ref('')
const busy = ref(false)
const confirmed = ref(false)
onMounted(async () => {
  try { status.value = await softwareUpdateApi.status() } catch (cause) { error.value = String(cause) }
})
async function perform(action: 'check' | 'download') {
  busy.value = true; error.value = ''; confirmed.value = false
  try { status.value = await softwareUpdateApi[action]() } catch (cause) { error.value = String(cause) }
  finally { busy.value = false }
}
async function install() {
  if (!confirmed.value || busy.value) return
  busy.value = true; error.value = ''
  try { await prepareAndInstallUpdate(softwareUpdateApi.install) }
  catch (cause) { error.value = String(cause) }
  finally { busy.value = false; confirmed.value = false }
}
</script>

<style scoped>
.software-update { display:flex; flex-direction:column; gap:1rem; align-items:flex-start }
pre { white-space:pre-wrap; overflow-wrap:anywhere }
.update-overlay { position:fixed; inset:0; z-index:2147483647; display:grid; place-items:center; background:var(--app-surface, #fff); color:var(--app-text, #222); font-size:1.2rem; padding:2rem }
</style>
