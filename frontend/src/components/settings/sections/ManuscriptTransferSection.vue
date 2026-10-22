<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <section class="transfer-section">
    <p>导出和导入作品元数据、正文及章节大纲。JSON稿件保留原始文本并逐章校验，导入会创建新作品。完整数据库、历史版本和设定请使用“备份与恢复”。</p>
    <div class="actions">
      <n-select v-model:value="selectedNovel" :options="novels" placeholder="选择导出的作品" :disabled="busy" aria-label="选择导出的作品" />
      <n-button :disabled="busy || !selectedNovel" @click="exportManuscript">导出JSON稿件</n-button>
    </div>
    <label>选择 JSON 稿件（兼容旧版格式）（最大32MB）<input type="file" accept=".json,application/json" :disabled="busy" @change="selectFile" /></label>
    <p v-if="filename">文件 {{ filename }}</p>
    <n-button :disabled="busy || !raw" :loading="busy" @click="previewFile">校验并预览导入</n-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul v-if="validationErrors.length"><li v-for="item in validationErrors" :key="item">{{ item }}</li></ul>
    <article v-if="preview" class="preview">
      <h4>{{ preview.novel.title }} · {{ preview.receipt ? '已导入' : preview.expired ? '预览已过期' : '已通过稿件校验' }}</h4>
      <p>作者 {{ preview.novel.author }} · 章节 {{ preview.validation.chapter_count }} · 正文字符 {{ preview.validation.content_character_count }} · 空稿 {{ preview.validation.empty_chapter_count }}</p>
      <p v-for="warning in preview.validation.warnings" :key="warning">{{ warning }}</p>
      <details><summary>逐章校验结果</summary><p v-for="chapter in preview.validation.chapters" :key="chapter.number">第 {{ chapter.number }} 章 {{ chapter.title }} · {{ chapter.characters }} 字符 · {{ chapter.content_sha256 }}</p></details>
      <div class="actions"><n-button :disabled="busy" @click="downloadReport">保存校验报告</n-button><n-button :disabled="busy" @click="refreshReceipt">刷新导入回执</n-button><n-button v-if="preview.status === 'pending' && !preview.expired" type="primary" :disabled="busy" @click="confirmImport">确认导入为新作品</n-button></div>
      <n-alert v-if="preview.receipt" type="success" :show-icon="false">已创建新作品 {{ preview.receipt.title }}（{{ preview.receipt.novel_id }}），正文 {{ preview.receipt.chapter_count }} 章。可到书库打开；原作品保留。</n-alert>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NSelect, useDialog, useMessage } from 'naive-ui'
import { manuscriptApi, type ManuscriptPreview } from '../../../api/manuscript'
import { novelApi } from '../../../api/novel'
const dialog = useDialog(), message = useMessage()
const novels = ref<{ label: string; value: string }[]>([]), selectedNovel = ref<string | null>(null)
const raw = ref(''), filename = ref(''), busy = ref(false), error = ref(''), validationErrors = ref<string[]>([])
const preview = ref<ManuscriptPreview | null>(null)
const recoveryKey = 'plotpilot:manuscript-import-preview'
let generation = 0
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob), anchor = document.createElement('a')
  anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function showError(exc: unknown) {
  error.value = exc instanceof Error ? exc.message : '稿件操作失败'
  const detail = (exc as { response?: { data?: { detail?: { errors?: string[] } } } })?.response?.data?.detail
  if (Array.isArray(detail?.errors)) validationErrors.value = detail.errors
}
async function selectFile(event: Event) {
  const id = ++generation, file = (event.target as HTMLInputElement).files?.[0]
  raw.value = ''; preview.value = null; error.value = ''; validationErrors.value = []; filename.value = file?.name || ''
  if (!file) return
  if (file.size > 32 * 1024 * 1024) { error.value = '稿件文件超过32MB'; return }
  try { const value = await file.text(); if (id === generation) raw.value = value }
  catch (exc) { if (id === generation) showError(exc) }
}
async function previewFile() {
  if (busy.value || !raw.value) return
  const id = ++generation
  busy.value = true; error.value = ''; validationErrors.value = []; preview.value = null
  try {
    const result = await manuscriptApi.preview(raw.value)
    if (id === generation) { preview.value = result; localStorage.setItem(recoveryKey, result.preview_id) }
  } catch (exc) { if (id === generation) showError(exc) }
  finally { busy.value = false }
}
function confirmImport() {
  const item = preview.value
  if (!item || busy.value) return
  dialog.warning({ title: '将校验稿件导入为新作品？', content: `创建「${item.novel.title}」，导入${item.validation.chapter_count}章。保留章节编号与正文，原作品保留。`, positiveText: '创建新作品并导入', negativeText: '取消', onPositiveClick: async () => {
    if (preview.value?.preview_id !== item.preview_id || busy.value) return false
    busy.value = true; error.value = ''
    try { const receipt = await manuscriptApi.confirm(item); preview.value = { ...item, status: 'imported', receipt }; await loadNovels(); message.success('稿件已验证导入，可从书库打开新作品') }
    catch (exc) { showError(exc); return false }
    finally { busy.value = false }
  } })
}
async function refreshReceipt() {
  const id = preview.value?.preview_id || localStorage.getItem(recoveryKey)
  if (!id || busy.value) return
  const request = ++generation; busy.value = true
  try { const result = await manuscriptApi.get(id); if (request === generation) preview.value = result }
  catch (exc) { if (request === generation) showError(exc) }
  finally { busy.value = false }
}
async function exportManuscript() {
  if (!selectedNovel.value || busy.value) return
  busy.value = true
  try { const blob = await novelApi.exportNovel(selectedNovel.value, 'json'); download(blob, `${selectedNovel.value}.taleweft.json`); message.success('稿件导出完成，已包含逐章校验信息') }
  catch (exc) { showError(exc) }
  finally { busy.value = false }
}
function downloadReport() { if (preview.value) download(new Blob([JSON.stringify(preview.value, null, 2)], { type: 'application/json' }), `manuscript-${preview.value.preview_id}-report.json`) }
async function loadNovels() { novels.value = (await novelApi.listNovels()).map(item => ({ label: item.title, value: item.id })) }
onMounted(() => { void loadNovels().catch(showError); if (localStorage.getItem(recoveryKey)) void refreshReceipt() })
</script>
<style scoped>.transfer-section { display: grid; gap: 14px; }.actions { display: flex; flex-wrap: wrap; gap: 8px; }.actions .n-select { max-width: 300px; }.preview { padding: 14px; border: 1px solid var(--border-color, #d1d5db); border-radius: 8px; overflow-wrap: anywhere; }</style>
