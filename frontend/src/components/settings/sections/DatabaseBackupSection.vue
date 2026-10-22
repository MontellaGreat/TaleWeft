<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <section class="backup-section">
    <p>备份作品数据库，包含已提交正文、历史版本、候选稿、结构、设定和审计记录。写作时也可备份。模型端点配置文件与数据库外的素材需另外保存。</p>
    <div class="actions"><n-button type="primary" :loading="busy" @click="createBackup">创建数据库备份</n-button><n-button :disabled="busy" @click="load">刷新备份记录</n-button></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-alert v-if="restored" type="success" :show-icon="false">已恢复并验证独立数据库副本：{{ restored.restored_database }}。恢复演练保留当前活动数据库；正文数量 {{ restored.validation.counts.chapters ?? 0 }}。</n-alert>
    <n-spin :show="loading">
      <n-empty v-if="!items.length" description="暂无数据库备份" />
      <article v-for="item in items" :key="item.backup_id" class="backup-card">
        <h4>{{ item.created_at }} · {{ item.status === 'verified' ? '已验证' : '等待重新验证' }}</h4>
        <p>作品 {{ item.validation.counts.novels ?? 0 }} · 正文 {{ item.validation.counts.chapters ?? 0 }} · 大小 {{ (item.database.size / 1024 / 1024).toFixed(2) }} MB</p>
        <p class="path">保存位置 {{ item.directory }}</p>
        <details><summary>查看校验结果</summary><p>完整性 {{ item.validation.integrity_check }} · 外键 {{ item.validation.foreign_key_check }}</p><p class="path">SHA-256 {{ item.database.sha256 }}</p></details>
        <div class="actions"><n-button size="small" :disabled="busy" @click="verify(item)">检查此备份</n-button><n-button size="small" :disabled="busy" @click="restore(item)">恢复到独立副本并验证</n-button><n-button size="small" type="warning" :disabled="busy" @click="requestRestore(item)">下次启动切换此备份</n-button></div>
      </article>
    </n-spin>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NEmpty, NSpin, useMessage } from 'naive-ui'
import { databaseBackupApi, type DatabaseBackup, type DatabaseRestoreCopy } from '../../../api/databaseBackup'
const message = useMessage()
const items = ref<DatabaseBackup[]>([]), restored = ref<DatabaseRestoreCopy | null>(null)
const loading = ref(false), busy = ref(false), error = ref('')
let generation = 0
async function load() {
  const id = ++generation; loading.value = true; error.value = ''
  try { const data = await databaseBackupApi.list(); if (id === generation) items.value = data }
  catch (exc) { if (id === generation) error.value = exc instanceof Error ? exc.message : '备份记录读取失败' }
  finally { if (id === generation) loading.value = false }
}
async function createBackup() {
  if (busy.value) return
  busy.value = true
  try { const item = await databaseBackupApi.create(); await load(); items.value = items.value.map(row => row.backup_id === item.backup_id ? item : row); message.success('备份已创建并验证') }
  catch (exc) { message.error(exc instanceof Error ? exc.message : '备份创建失败') }
  finally { busy.value = false }
}
async function verify(item: DatabaseBackup) {
  busy.value = true
  try { const result = await databaseBackupApi.verify(item.backup_id); items.value = items.value.map(row => row.backup_id === result.backup_id ? result : row); message.success('文件校验、完整性和外键检查通过') }
  catch (exc) { message.error(exc instanceof Error ? exc.message : '备份校验失败') }
  finally { busy.value = false }
}
async function restore(item: DatabaseBackup) {
  busy.value = true; restored.value = null
  try { restored.value = await databaseBackupApi.restoreCopy(item.backup_id); message.success('独立副本恢复演练通过') }
  catch (exc) { message.error(exc instanceof Error ? exc.message : '恢复演练失败') }
  finally { busy.value = false }
}
async function requestRestore(item: DatabaseBackup) {
  busy.value = true
  try {
    const result = await databaseBackupApi.requestRestore(item.backup_id, item.database.sha256)
    message.success(`恢复请求已保存：${result.request_id}。请关闭并重新启动叙织 完成切换。`)
  } catch (exc) { message.error(exc instanceof Error ? exc.message : '恢复请求失败') }
  finally { busy.value = false }
}
onMounted(() => { void load() })
</script>
<style scoped>.backup-section { display: grid; gap: 14px; }.actions { display: flex; flex-wrap: wrap; gap: 8px; }.backup-card { border: 1px solid var(--border-color, #d1d5db); border-radius: 8px; padding: 14px; margin-top: 10px; }.path { overflow-wrap: anywhere; font-size: 12px; }</style>
