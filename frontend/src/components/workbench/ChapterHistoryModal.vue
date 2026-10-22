<template>
  <n-modal
    :show="show"
    @update:show="emit('update:show', $event)"
    preset="card"
    title="章节历史版本"
    :mask-closable="!restoring"
    style="width: min(1040px, 92vw)"
  >
    <n-space vertical :size="12">
      <n-alert v-if="errorText" type="error">{{ errorText }}</n-alert>
      <n-alert v-if="latestJob?.cascade_status === 'unavailable'" type="warning">
        后续章节的关联重算尚未完成，请重跑章后关联分析。{{ latestJob.cascade_error }}
      </n-alert>
      <n-alert v-else-if="latestJob?.cascade_status === 'running'" type="info">
        正在按章节顺序重算关联内容，请等待全部章节完成。
      </n-alert>
      <n-alert v-if="latestJob?.status === 'unavailable'" type="warning">
        本章正文已保存，章后分析暂不可用；已完成的关联数据会保留。失败原因：{{ latestJob.last_error || '暂未提供详细信息' }}。可点击下方按钮手动重试。
      </n-alert>
      <n-alert v-else-if="latestJob && ['pending', 'running'].includes(latestJob.status)" type="info">
        本章正文已保存，章后分析正在排队或运行。
      </n-alert>
      <n-text depth="3">{{ restoresTitle ? '恢复会把历史章节名和正文一起保存为一个新版本；已有版本会继续保留。' : '恢复会把历史正文保存为一个新版本，并保留当前章节名；已有版本会继续保留。' }}</n-text>
      <n-space align="center">
        <n-text v-if="latestJob" depth="3">本章任务：{{ jobStatusLabel(latestJob.status) }} · 已尝试 {{ latestJob.attempts }} 次</n-text>
        <n-button size="small" :loading="requestingJob" :disabled="jobBusy || !allowRestore" @click="requestDerivation('rebuild_indexes')">重建正文与摘要索引</n-button>
        <n-button size="small" :loading="requestingJob" :disabled="jobBusy || !allowRestore" @click="requestDerivation('retry_aftermath')">{{ latestJob?.status === 'unavailable' ? '重试章后关联分析' : '重跑章后关联分析' }}</n-button>
      </n-space>
      <n-text depth="3">正文保存后会自动重算摘要、记忆与关联内容，并按章顺序复核后续已写章节；手工确认的记忆会保留。</n-text>
      <n-space align="center" :wrap="true">
        <n-select
          v-model:value="selectedRevision"
          @update:value="historyKind = 'version'"
          :options="versionOptions"
          :loading="loading"
          :disabled="restoring"
          placeholder="选择历史版本"
          style="width: min(600px, 75vw)"
        />
        <n-select
          v-if="legacyDrafts.length"
          v-model:value="selectedDraftId"
          @update:value="historyKind = 'draft'"
          :options="draftOptions"
          :loading="loading"
          :disabled="restoring"
          placeholder="选择旧草稿快照"
          style="width: min(420px, 75vw)"
        />
        <n-button size="small" :loading="loading" :disabled="restoring" @click="loadHistory()">刷新</n-button>
        <n-button v-if="hasMore" size="small" :loading="loadingMore" :disabled="restoring" @click="loadHistory(true)">加载更早版本</n-button>
      </n-space>
      <n-text v-if="selectedTarget" depth="3">
        {{ restoresTitle ? `所选历史章节名：${selectedVersion?.title}` : historyKind === 'draft' ? '所选旧草稿未记录章节名，恢复时保留当前章节名。' : '所选历史版本未记录章节名，恢复时保留当前章节名。' }}
      </n-text>
      <n-spin :show="diffLoading">
        <n-tabs type="line" animated>
          <n-tab-pane name="diff" tab="与当前保存版本比较">
            <pre class="version-preview">{{ conflictDiff || diffText || '正文相同，或尚未选择版本。' }}</pre>
          </n-tab-pane>
          <n-tab-pane name="content" tab="历史正文">
            <pre class="version-preview">{{ selectedContent || '此版本正文为空。' }}</pre>
          </n-tab-pane>
        </n-tabs>
      </n-spin>
      <n-space justify="end">
        <n-button :disabled="restoring" @click="emit('update:show', false)">关闭</n-button>
        <n-button
          type="primary"
          :loading="restoring"
          :disabled="!selectedTarget || !currentVersion || !allowRestore || (historyKind === 'version' && selectedRevision === currentVersion.revision) || !!conflictDiff"
          @click="restoreSelected"
        >{{ restoresTitle ? '恢复章节名与正文为新版本' : '恢复此正文为新版本' }}</n-button>
      </n-space>
    </n-space>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, ref, nextTick, watch, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'
import { chapterApi, getChapterRevisionConflict, type ChapterContentVersion, type ChapterDraft, type ChapterDTO, type ChapterVersion, type ChapterDerivationJob } from '../../api/chapter'
import { formatApiError } from '../../utils/apiError'

const props = withDefaults(defineProps<{
  show: boolean
  slug: string
  chapterNumber: number | null
  currentVersion: ChapterVersion | null
  allowRestore?: boolean
  beforeRestore?: () => Promise<boolean>
}>(), { allowRestore: true })
const emit = defineEmits<{
  'update:show': [value: boolean]
  restored: [chapter: ChapterDTO]
}>()
const message = useMessage()
const versions = ref<ChapterContentVersion[]>([])
const legacyDrafts = ref<ChapterDraft[]>([])
const jobs = ref<ChapterDerivationJob[]>([])
const historyKind = ref<'version' | 'draft'>('version')
const selectedRevision = ref<number | null>(null)
const selectedDraftId = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const diffLoading = ref(false)
const restoring = ref(false)
const hasMore = ref(false)
const errorText = ref('')
const diffText = ref('')
const conflictDiff = ref('')
const requestingJob = ref(false)
let jobPollTimer: ReturnType<typeof setTimeout> | null = null
let generation = 0
let diffGeneration = 0
const selectedVersion = computed(() => versions.value.find(version => version.revision === selectedRevision.value) ?? null)
const selectedDraft = computed(() => legacyDrafts.value.find(draft => draft.id === selectedDraftId.value) ?? null)
const selectedTarget = computed(() => historyKind.value === 'draft' ? selectedDraft.value : selectedVersion.value)
const selectedContent = computed(() => selectedTarget.value?.content || '')
const restoresTitle = computed(() => historyKind.value === 'version' && !!selectedVersion.value?.title?.trim())
const latestJob = computed(() => jobs.value.find(job => job.revision === props.currentVersion?.revision) ?? null)
const jobBusy = computed(() => requestingJob.value || restoring.value || latestJob.value?.status === 'pending' || latestJob.value?.status === 'running' || latestJob.value?.cascade_status === 'running')
const versionOptions = computed(() => versions.value.map(version => ({
  value: version.revision,
  label: `版本 ${version.revision} · ${version.title?.trim() || '未记录章节名'} · ${version.created_at} · ${sourceLabel(version.source)} · ${version.content.length} 字`,
})))
const draftOptions = computed(() => legacyDrafts.value.map(draft => ({
  value: draft.id,
  label: `旧草稿 · ${draft.created_at} · ${sourceLabel(draft.source)} · ${draft.content.length} 字`,
})))

function sourceLabel(source: string) {
  if (source.startsWith('restore:')) return `恢复自版本 ${source.slice(8)}`
  return ({ manual: '编辑保存', initial: '初始稿', baseline: '原始稿', legacy: '其他保存' } as Record<string, string>)[source] || source
}

function jobStatusLabel(status: string) {
  return ({ pending: '已排队', running: '运行中', completed: '已完成', unavailable: '失败，可重试', superseded: '已被新正文替代' } as Record<string, string>)[status] || status
}

function clearJobPoll() {
  if (jobPollTimer) clearTimeout(jobPollTimer)
  jobPollTimer = null
}

function scheduleJobPoll() {
  clearJobPoll()
  if (!props.show || !jobs.value.some(job => ['pending', 'running'].includes(job.status) || job.cascade_status === 'running')) return
  jobPollTimer = setTimeout(() => { void refreshJobs() }, 1500)
}

async function refreshJobs() {
  if (!props.show || props.chapterNumber == null) return
  const request = generation, slug = props.slug, chapter = props.chapterNumber
  try {
    const result = await chapterApi.listDerivationJobs(slug, chapter)
    if (request !== generation || !props.show || slug !== props.slug || chapter !== props.chapterNumber) return
    jobs.value = result
    scheduleJobPoll()
  } catch (error) {
    if (request === generation) errorText.value = formatApiError(error, '任务状态刷新失败，请点击刷新')
  }
}

async function requestDerivation(action: 'retry_aftermath' | 'rebuild_indexes') {
  if (props.chapterNumber == null || jobBusy.value || !props.allowRestore) return
  const request = generation, slug = props.slug, chapter = props.chapterNumber
  requestingJob.value = true
  try {
    if (props.beforeRestore && !await props.beforeRestore()) return
    if (request !== generation || slug !== props.slug || chapter !== props.chapterNumber) return
    const result = await chapterApi.requestDerivation(slug, chapter, action)
    if (request !== generation || slug !== props.slug || chapter !== props.chapterNumber) return
    jobs.value = [result, ...jobs.value.filter(job => job.job_id !== result.job_id)]
    message.success(action === 'rebuild_indexes' ? '索引重建已排队' : '章后分析重试已排队')
    scheduleJobPoll()
  } catch (error) {
    if (request === generation) errorText.value = formatApiError(error, '任务未安排，请刷新后重试')
  } finally {
    if (request === generation) requestingJob.value = false
  }
}

async function loadHistory(more = false) {
  if (props.chapterNumber == null || !props.show) return
  const request = ++generation
  const novelId = props.slug, chapterNumber = props.chapterNumber
  if (more) loadingMore.value = true
  else loading.value = true
  errorText.value = ''
  conflictDiff.value = ''
  try {
    const draftPromise = typeof (chapterApi as typeof chapterApi & { listDrafts?: typeof chapterApi.listDrafts }).listDrafts === 'function'
      ? chapterApi.listDrafts(novelId, chapterNumber)
      : Promise.resolve([] as ChapterDraft[])
    const [page, derivationJobs, drafts] = await Promise.all([
      chapterApi.listVersions(novelId, chapterNumber, more ? versions.value.at(-1)?.revision : undefined),
      chapterApi.listDerivationJobs(novelId, chapterNumber),
      draftPromise,
    ])
    if (request !== generation || novelId !== props.slug || chapterNumber !== props.chapterNumber) return
    versions.value = more ? [...versions.value, ...page] : page
    if (!more) legacyDrafts.value = drafts
    jobs.value = derivationJobs
    scheduleJobPoll()
    hasMore.value = page.length === 50
    if (!versions.value.some(version => version.revision === selectedRevision.value)) {
      selectedRevision.value = page[0]?.revision ?? null
    }
    void loadDiff()
  } catch (error) {
    if (request === generation) errorText.value = formatApiError(error, '加载历史版本失败')
  } finally {
    if (request === generation) { loading.value = false; loadingMore.value = false }
  }
}

async function loadDiff() {
  if (!props.show || props.chapterNumber == null || !selectedTarget.value) return
  const request = ++diffGeneration
  const novelId = props.slug, chapterNumber = props.chapterNumber
  diffLoading.value = true
  diffText.value = ''
  try {
    const result = historyKind.value === 'draft' && selectedDraftId.value
      ? await chapterApi.diffDraft(novelId, chapterNumber, selectedDraftId.value, props.currentVersion?.revision)
      : await chapterApi.diffVersion(novelId, chapterNumber, selectedRevision.value as number, props.currentVersion?.revision)
    if (request === diffGeneration && novelId === props.slug && chapterNumber === props.chapterNumber) diffText.value = result.diff
  } catch (error) {
    if (request === diffGeneration) errorText.value = formatApiError(error, '加载版本差异失败')
  } finally {
    if (request === diffGeneration) diffLoading.value = false
  }
}

async function restoreSelected() {
  if (props.chapterNumber == null || !selectedTarget.value || !props.allowRestore || restoring.value) return
  restoring.value = true
  const request = generation, novelId = props.slug, chapterNumber = props.chapterNumber, revision = selectedRevision.value
  try {
    if (props.beforeRestore && !await props.beforeRestore()) return
    await nextTick()
    if (request !== generation || novelId !== props.slug || chapterNumber !== props.chapterNumber || !props.currentVersion) return
    const saved = historyKind.value === 'draft' && selectedDraftId.value
      ? await chapterApi.restoreDraft(novelId, chapterNumber, selectedDraftId.value, { ...props.currentVersion })
      : await chapterApi.restoreVersion(novelId, chapterNumber, selectedRevision.value as number, { ...props.currentVersion })
    if (request !== generation || novelId !== props.slug || chapterNumber !== props.chapterNumber) return
    emit('restored', saved)
    message.success(`${restoresTitle.value ? '历史章节名与正文' : historyKind.value === 'draft' ? '旧草稿正文' : '历史正文'}已恢复为版本 ${saved.revision}；章后分析已排队`)
    await nextTick()
    await loadHistory()
  } catch (error) {
    if (request !== generation) return
    const conflict = getChapterRevisionConflict(error)
    if (conflict) {
      errorText.value = `服务器已更新到版本 ${conflict.current.revision}，恢复未执行。请关闭历史面板并重新加载章节后比较。`
      conflictDiff.value = conflict.diff || '正文相同，但版本已经变化。'
    } else errorText.value = formatApiError(error, '恢复失败，当前章节与历史均已保留')
  } finally {
    restoring.value = false
  }
}

watch(() => [props.show, props.slug, props.chapterNumber] as const, ([show]) => {
  generation += 1
  clearJobPoll()
  requestingJob.value = false
  diffGeneration += 1
  versions.value = []
  jobs.value = []
  selectedRevision.value = null
  selectedDraftId.value = null
  historyKind.value = 'version'
  diffText.value = ''
  conflictDiff.value = ''
  errorText.value = ''
  if (show) void loadHistory()
}, { immediate: true })
watch(() => [historyKind.value, selectedRevision.value, selectedDraftId.value, props.currentVersion?.revision], () => { conflictDiff.value = ''; void loadDiff() })
onBeforeUnmount(() => { generation += 1; diffGeneration += 1; clearJobPoll() })
</script>

<style scoped>
.version-preview {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 55vh;
  overflow: auto;
  padding: 12px;
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}
</style>
