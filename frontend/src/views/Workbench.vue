<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <div class="workbench" :class="{ 'is-focused': focusMode, 'is-chapters-open': chaptersOpen, 'is-reference-open': !rightCollapsed }">
    <nav class="desk-rail" aria-label="应用导航">
      <button class="desk-brand" title="叙织 · TaleWeft · 返回书库" aria-label="叙织书库" @click="goHome"><img src="/taleweft-mark.svg" alt="" /></button>
      <button class="rail-action" title="作品书库" @click="goHome"><n-icon :size="21"><LibraryOutline /></n-icon><span>书库</span></button>
      <button class="rail-action active" title="写作书桌" @click="activateDesk('writing')"><n-icon :size="21"><CreateOutline /></n-icon><span>写作</span></button>
      <button class="rail-action" title="运行中心" @click="openOperations(false)"><n-icon :size="21"><PulseOutline /></n-icon><span>运行</span></button>
      <span class="rail-divider" />
      <button class="rail-action" title="模型引擎与思考程度" @click="appSettingsShell.open('engine')"><n-icon :size="21"><HardwareChipOutline /></n-icon><span>模型</span></button>
      <button class="rail-action" title="提示词广场" @click="statsTopBarRef?.openAiTool('plaza')"><n-icon :size="21"><SparklesOutline /></n-icon><span>提示词</span></button>
      <button class="rail-action" title="作品统计仪表盘" @click="openMetrics"><n-icon :size="21"><BarChartOutline /></n-icon><span>统计</span></button>
      <span class="rail-spacer" />
      <button class="rail-action" title="应用设置" @click="appSettingsShell.open()"><n-icon :size="21"><SettingsOutline /></n-icon><span>设置</span></button>
      <button class="rail-action" title="关于与帮助" @click="appSettingsShell.open('about')"><n-icon :size="21"><HelpCircleOutline /></n-icon><span>帮助</span></button>
    </nav>
    <div class="desk-shell">
      <header class="desk-heading">
        <div class="desk-book"><span class="desk-eyebrow">TALEWEFT / 创作空间</span><strong :title="bookTitle">{{ bookTitle || '正在打开作品…' }}</strong></div>
        <nav class="desk-navigation" aria-label="作品工作区">
          <button v-for="item in deskNavigation" :key="item.id" :class="{ selected: activeDesk === item.id }" :aria-pressed="activeDesk === item.id" @click="activateDesk(item.id)">{{ item.label }}</button>
        </nav>
        <div class="desk-heading-actions">
          <button class="desk-plain-button chapters-toggle" :aria-expanded="chaptersOpen" @click="toggleChapters">章节</button>
          <button class="desk-plain-button" :aria-pressed="focusMode" @click="focusMode = !focusMode">{{ focusMode ? '退出专注' : '专注' }}</button>
          <button class="desk-plain-button" :aria-expanded="!rightCollapsed" @click="toggleRight">资料</button>
        </div>
      </header>
    <StatsTopBar ref="statsTopBarRef" :slug="slug" @open-settings="appSettingsShell.open()" />

    <n-spin :show="pageLoading" class="workbench-spin" description="加载工作台…">
      <div class="workbench-inner">
        <n-split
          direction="horizontal"
          class="desk-chapter-split"
          pane1-class="desk-chapter-pane"
          pane2-class="desk-writing-pane"
          :min="WORKBENCH_SPLIT.sidebarMin"
          :max="WORKBENCH_SPLIT.sidebarMax"
          :default-size="WORKBENCH_SPLIT.sidebarDefault"
        >
          <template #1>
            <div class="desk-chapter-container"><button class="mobile-panel-close" @click="chaptersOpen = false">关闭章节目录 ×</button><ChapterList
              ref="chapterListRef"
              :slug="slug"
              :chapters="chapters"
              :current-chapter-id="currentChapterId"
              :generation-prefs="generationPrefs"
              :writing-chapter-number="writingChapterNumber"
              :writing-pipeline-step="writingPipelineStep"
              @select="onSidebarChapterSelect"
              @back="goHome"
              @refresh="handleChapterUpdated"
              @plan-act="handlePlanAct"
            /></div>
          </template>

          <template #2>
            <div class="wb-main-split" :class="{ 'wb-right-collapsed': rightCollapsed }">
              <n-split
                direction="horizontal"
                pane1-class="desk-manuscript-pane"
                pane2-class="desk-reference-pane"
                :disabled="rightCollapsed"
                :min="WORKBENCH_SPLIT.mainMin"
                :max="WORKBENCH_SPLIT.mainMax"
                :default-size="WORKBENCH_SPLIT.mainDefault"
              >
                <template #1>
                  <WorkArea
                    ref="workAreaRef"
                    :slug="slug"
                    :book-title="bookTitle"
                    :chapters="chapters"
                    :current-chapter-id="currentChapterId"
                    :chapter-content="chapterContent"
                    :chapter-version="chapterVersion"
                    :chapter-loading="chapterLoading"
                    :generation-prefs="generationPrefs"
                    @chapter-updated="handleChapterUpdated"
                    @select-chapter="handleChapterSelect"
                  />
                </template>

                <template #2>
                  <button v-if="rightCollapsed" class="wb-right-strip" aria-label="展开资料面板" title="展开资料面板" @click="toggleRight"><span>◀</span><span>资料</span></button>
                  <SettingsPanel
                    v-show="!rightCollapsed"
                    :slug="slug"
                    :current-panel="rightPanel"
                    :current-chapter="currentChapter"
                    :generation-prefs="generationPrefs"
                    @update:current-panel="onSettingsPanelChange"
                    @collapse="toggleRight"
                  />
                </template>
              </n-split>
            </div>
          </template>
        </n-split>
      </div>
    </n-spin>
    </div>

    <!-- 幕→章 AI 规划弹层 -->
    <ActPlanningModal
      v-model:show="showActPlanning"
      :act-id="actPlanningId"
      :act-title="actPlanningTitle"
      @confirmed="handleChapterUpdated"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch, defineAsyncComponent, type ComponentPublicInstance } from 'vue'
import { useRoute, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useDebouncedTask } from '../composables/useDebouncedTask'
import { useWorkbench } from '../composables/useWorkbench'
import { useStatsStore } from '../stores/statsStore'
import { useWorkbenchRefreshStore } from '../stores/workbenchRefreshStore'
import { useAppSettingsShellStore } from '../stores/appSettingsShellStore'
import { useAutopilotWorkspaceStore } from '../stores/autopilotWorkspaceStore'
import { LibraryOutline, CreateOutline, PulseOutline, HardwareChipOutline, SparklesOutline, BarChartOutline, SettingsOutline, HelpCircleOutline } from '@vicons/ionicons5'
import StatsTopBar from '../components/stats/StatsTopBar.vue'
import ChapterList from '../components/workbench/ChapterList.vue'
import WorkArea from '../components/workbench/WorkArea.vue'
import SettingsPanel from '../components/workbench/SettingsPanel.vue'
import {
  WORKBENCH_CHAPTER_DESK_CHANGE_EVENT,
  WORKBENCH_OPEN_SETTINGS_PANEL_EVENT,
  WORKBENCH_GENERATION_PREFS_UPDATED_EVENT,
  isWorkbenchSettingsPanelName,
} from '../workbench/deskEvents'
import { WORKBENCH_SPLIT } from '../design/layoutDensity'
import { storageKeys } from '@/config/storageKeys'
import { runtimePerformance } from '@/config/performance'
import { readStorageBoolean, writeStorageBoolean } from '@/utils/storage'

const ActPlanningModal = defineAsyncComponent(() => import('../components/workbench/ActPlanningModal.vue'))

const route = useRoute()
const message = useMessage()
const statsStore = useStatsStore()
const workbenchRefresh = useWorkbenchRefreshStore()
const appSettingsShell = useAppSettingsShellStore()
const workspace = useAutopilotWorkspaceStore()
const statsTopBarRef = ref<{ openAiTool: (key: string) => void } | null>(null)
const focusMode = ref(false)
const chaptersOpen = ref(false)
const activeDesk = ref('writing')
const deskNavigation = [
  { id: 'overview', label: '概览' }, { id: 'writing', label: '写作' },
  { id: 'reference', label: '设定' }, { id: 'planning', label: '规划' },
  { id: 'review', label: '审阅' }, { id: 'workflow', label: '工作流' },
]

function revealPanel(panel: string) {
  rightPanel.value = panel
  rightCollapsed.value = false
  focusMode.value = false
  writeStorageBoolean(storageKeys.workbenchRightPanelCollapsed, false)
}
function openOperations(dag: boolean) {
  activeDesk.value = 'workflow'
  if (dag) workspace.openDag()
  else workspace.openMonitor()
  workAreaRef.value?.openManagedWorkspace?.()
}
function openMetrics() {
  workspace.setTab('dashboard')
  workAreaRef.value?.openManagedWorkspace?.()
}
function activateDesk(id: string) {
  activeDesk.value = id
  if (id === 'writing') workAreaRef.value?.ensureAssistedMode?.()
  else if (id === 'overview') revealPanel('narrative-brief')
  else if (id === 'reference') revealPanel('bible')
  else if (id === 'planning') chapterListRef.value?.openMacroPlan?.()
  else if (id === 'review') appSettingsShell.open('quality-rules')
  else if (id === 'workflow') openOperations(true)
}

const slug = computed(() => String(route.params.slug ?? ''))

const chapterListRef = ref<ComponentPublicInstance<{ refreshStoryTree: () => void; openMacroPlan: () => void }> | null>(null)
const workAreaRef = ref<ComponentPublicInstance<{
  ensureAssistedMode: () => void
  openManagedWorkspace: () => void
  flushPendingEditor: () => Promise<boolean>
  streamingChapterNumber: import('vue').Ref<number | null>
  writingPipelineStep: import('vue').ComputedRef<number | null>
}> | null>(null)

const writingChapterNumber = computed(() => workAreaRef.value?.streamingChapterNumber?.value ?? null)
const writingPipelineStep = computed(() => workAreaRef.value?.writingPipelineStep?.value ?? null)

async function onSidebarChapterSelect(chapterId: number, title = '') {
  await handleChapterSelect(chapterId, title)
  workAreaRef.value?.ensureAssistedMode?.()
  chaptersOpen.value = false
  activeDesk.value = 'writing'
}

async function runChapterDeskReload() {
  await loadDesk()
  void statsStore.loadBookStats(slug.value, true).catch(() => {})
  window.dispatchEvent(new CustomEvent('plotpilot:bible-panel:soft-reload'))
  chapterListRef.value?.refreshStoryTree?.()
  workbenchRefresh.bumpAfterChapterDeskChange()
}

/** 合并短时间内的多次「整桌刷新」：全托管状态抖动 / 多源 emit 时只拉一次 API，减轻闪烁与日志刷屏 */
const chapterDeskReload = useDebouncedTask(
  runChapterDeskReload,
  () => runtimePerformance.workbench.deskReloadDebounceMs,
  {
    onError: () => {
      message.error('刷新工作台失败，请检查网络与后端是否已启动')
    },
  },
)

const handleChapterUpdated = () => {
  chapterDeskReload.schedule()
}

function onDeskChangeSignalFromPanels() {
  handleChapterUpdated()
}

function onOpenSettingsPanelFromChild(e: Event) {
  const panel = (e as CustomEvent<{ panel?: string }>).detail?.panel
  if (typeof panel === 'string' && isWorkbenchSettingsPanelName(panel)) {
    revealPanel(panel)
  }
}

// 幕→章 规划弹层
const showActPlanning = ref(false)
const actPlanningId = ref('')
const actPlanningTitle = ref('')

const handlePlanAct = (actId: string, actTitle: string) => {
  actPlanningId.value = actId
  actPlanningTitle.value = actTitle
  showActPlanning.value = true
}

const rightCollapsed = ref(readStorageBoolean(storageKeys.workbenchRightPanelCollapsed))

function toggleRight() {
  if (focusMode.value) {
    focusMode.value = false
    rightCollapsed.value = false
  } else rightCollapsed.value = !rightCollapsed.value
  writeStorageBoolean(storageKeys.workbenchRightPanelCollapsed, rightCollapsed.value)
}
function toggleChapters() {
  chaptersOpen.value = !chaptersOpen.value
  if (chaptersOpen.value) focusMode.value = false
}

const {
  bookTitle,
  chapters,
  generationPrefs,
  rightPanel,
  pageLoading,
  bookMeta,
  currentJobId,
  currentChapterId,
  chapterContent,
  chapterVersion,
  chapterLoading,
  setRightPanel,
  loadDesk,
  reloadDeskForSlugChange,
  goHome,
  goToChapter,
  handleChapterSelect,
} = useWorkbench({
  slug,
  beforeChapterChange: () => workAreaRef.value?.flushPendingEditor?.() ?? true,
})

onBeforeRouteLeave(() => workAreaRef.value?.flushPendingEditor?.() ?? true)
onBeforeRouteUpdate(() => workAreaRef.value?.flushPendingEditor?.() ?? true)

const currentChapter = computed(() => {
  if (!currentChapterId.value) return null
  return chapters.value.find(ch => ch.id === currentChapterId.value) || null
})

function onSettingsPanelChange(panel: string) {
  rightPanel.value = panel
}

function parseChapterQuery(q: unknown): number | null {
  if (q == null || q === '') return null
  const raw = Array.isArray(q) ? q[0] : q
  const n = Number(raw)
  return !Number.isNaN(n) && n >= 1 ? n : null
}

async function syncChapterFromRoute() {
  const n = parseChapterQuery(route.query.chapter)
  if (n != null) {
    await goToChapter(n)
  }
}

function onGenerationPrefsUpdated() {
  void loadDesk()
  chapterListRef.value?.refreshStoryTree?.()
}

onMounted(async () => {
  window.addEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.addEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.addEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  try {
    await loadDesk()
    await syncChapterFromRoute()
  } catch {
    message.error('加载失败，请检查网络与后端是否已启动')
    bookTitle.value = slug.value
  } finally {
    pageLoading.value = false
  }
})

onUnmounted(() => {
  window.removeEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.removeEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.removeEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  chapterDeskReload.cancel()
})

watch(
  () => route.query.chapter,
  () => {
    void syncChapterFromRoute()
  }
)

watch(
  slug,
  async (next, prev) => {
    if (!next || prev === next) return
    try {
      await reloadDeskForSlugChange()
      await syncChapterFromRoute()
      void statsStore.loadBookStats(next, true).catch(() => {})
      chapterListRef.value?.refreshStoryTree?.()
      workbenchRefresh.bumpAfterChapterDeskChange()
    } catch {
      message.error('切换作品失败，请检查网络与后端是否已启动')
      bookTitle.value = next
    }
  }
)
</script>

<style scoped>
.workbench { height:100dvh; min-height:0; overflow:hidden; display:flex; background:var(--app-page-bg); }
.desk-rail { width:68px; flex:none; display:flex; flex-direction:column; align-items:center; gap:6px; padding:16px 6px 10px; box-sizing:border-box; background:var(--app-surface); border-right:1px solid var(--app-border); }
.desk-brand { display:grid; place-items:center; width:38px; height:38px; padding:0; overflow:hidden; border:0; border-radius:12px; background:#102c32; cursor:pointer; margin-bottom:18px; }
.desk-brand img { display:block; width:100%; height:100%; object-fit:contain; }
.rail-action { width:54px; min-height:52px; border:0; border-radius:10px; background:transparent; color:var(--app-text-muted); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; cursor:pointer; font:inherit; font-size:11px; }
.rail-action:hover,.rail-action.active { background:var(--color-brand-light); color:var(--color-brand); }
.rail-divider { width:24px; border-bottom:1px solid var(--app-border); margin:4px 0; }
.rail-spacer { flex:1; }
.desk-shell { min-width:0; min-height:0; flex:1; display:flex; flex-direction:column; }
.desk-heading { min-height:70px; flex:none; display:flex; align-items:center; gap:24px; padding:0 24px; border-bottom:1px solid var(--app-border); background:var(--app-surface); }
.desk-book { display:flex; flex-direction:column; gap:4px; min-width:100px; max-width:230px; }
.desk-eyebrow { font-size:9px; letter-spacing:1.6px; color:var(--app-text-muted); }
.desk-book strong { font-size:16px; font-weight:600; white-space:nowrap; text-overflow:ellipsis; overflow:hidden; }
.desk-navigation { display:flex; align-self:stretch; align-items:center; gap:5px; flex:1; }
.desk-navigation button,.desk-plain-button { border:0; color:var(--app-text-secondary); background:transparent; padding:9px 13px; font:inherit; font-size:12px; cursor:pointer; border-radius:7px; white-space:nowrap; }
.desk-navigation button.selected { color:var(--color-brand); background:var(--color-brand-light); font-weight:650; }
.desk-navigation button:hover,.desk-plain-button:hover { background:var(--app-surface-subtle); }
.desk-heading-actions { display:flex; gap:4px; }
.desk-plain-button { border:1px solid var(--app-border); padding:6px 10px; }
.chapters-toggle,.mobile-panel-close { display:none; }
.workbench-spin { flex:1; min-height:0; overflow:hidden; display:flex; flex-direction:column; }
.workbench-spin :deep(.n-spin-content) { flex:1; min-height:0; height:auto; display:flex; flex-direction:column; overflow:hidden; }
.workbench-inner { flex:1; min-height:0; display:flex; overflow:hidden; }
.workbench-inner :deep(.n-split) { min-height:0; height:100%; width:100%; }
.workbench-inner :deep(.desk-chapter-pane),.workbench-inner :deep(.desk-writing-pane),.workbench-inner :deep(.desk-manuscript-pane),.workbench-inner :deep(.desk-reference-pane) { min-height:0; min-width:0; overflow:hidden; }
.desk-chapter-container { height:100%; min-height:0; }
.wb-main-split { height:100%; width:100%; overflow:hidden; }
.wb-right-collapsed :deep(.desk-manuscript-pane) { flex:1 1 0!important; max-width:none!important; }
.wb-right-collapsed :deep(.desk-reference-pane) { flex:0 0 32px!important; width:32px!important; min-width:0!important; overflow:hidden; }
.wb-right-strip { height:100%; width:32px; display:flex; gap:12px; flex-direction:column; align-items:center; justify-content:center; cursor:pointer; background:var(--app-surface); border:0; border-left:1px solid var(--app-border); color:var(--app-text-muted); font-size:11px; }
.wb-right-strip:hover { color:var(--color-brand); background:var(--color-brand-light); }
.is-focused :deep(.desk-chapter-pane),.is-focused :deep(.desk-reference-pane) { flex:0 0 0!important; width:0!important; }
.is-focused :deep(.desk-manuscript-pane),.is-focused :deep(.desk-writing-pane) { flex:1 1 0!important; }
.is-focused :deep(.n-split__resize-trigger-wrapper) { display:none; }
button:focus-visible { outline:2px solid var(--color-brand); outline-offset:3px; }
@media(max-width:1200px) {
 .desk-heading { padding:0 14px; gap:14px; }
 .desk-book { max-width:180px; }
 .desk-navigation { gap:0; }
 .desk-navigation button { padding:8px 9px; }
 .workbench-inner :deep(.desk-reference-pane) { position:fixed; z-index:25; inset:112px 0 0 auto; width:min(420px,calc(100vw - 68px))!important; background:var(--app-surface); box-shadow:-15px 0 35px #0002; }
 .wb-right-collapsed :deep(.desk-reference-pane),.is-focused :deep(.desk-reference-pane) { display:none; }
 .workbench-inner :deep(.desk-manuscript-pane) { flex:1 1 0!important; }
 .wb-main-split > :deep(.n-split > .n-split__resize-trigger-wrapper) { display:none; }
}
@media(max-width:900px) {
 .desk-book { max-width:170px; }
 .desk-navigation { overflow-x:auto; }
 .desk-navigation button { padding:8px; }
 .desk-heading { flex-wrap:wrap; gap:8px; padding:10px 14px; }
 .desk-navigation { order:3; width:100%; flex-basis:100%; }
 .desk-heading-actions { margin-left:auto; }
 .chapters-toggle { display:block; }
 .workbench-inner :deep(.desk-chapter-pane) { position:fixed; z-index:26; inset:150px auto 0 68px; width:270px!important; max-width:calc(100vw - 68px); box-shadow:15px 0 35px #0002; display:none; background:var(--app-surface); }
 .is-chapters-open :deep(.desk-chapter-pane) { display:block; }
 .desk-chapter-container { display:flex; flex-direction:column; }
 .desk-chapter-container :deep(.sidebar) { flex:1; min-height:0; }
 .mobile-panel-close { display:block; flex:none; border:0; padding:12px; color:var(--color-brand); background:var(--color-brand-light); cursor:pointer; }
 .workbench-inner :deep(.desk-writing-pane) { flex:1 1 0!important; }
 .desk-chapter-split > :deep(.n-split__resize-trigger-wrapper) { display:none; }
 .workbench-inner :deep(.desk-reference-pane) { top:150px; }
}
@media(max-width:600px) {
 .desk-rail { width:52px; padding:12px 3px; gap:3px; }
 .rail-action { width:44px; min-height:48px; font-size:10px; }
 .desk-brand { width:34px; height:34px; margin-bottom:9px; }
 .desk-heading { padding:9px 10px; }
 .desk-book { max-width:145px; }
 .desk-book strong { font-size:14px; }
 .desk-eyebrow { font-size:8px; letter-spacing:.6px; }
 .desk-plain-button { padding:5px; font-size:11px; }
 .workbench-inner :deep(.desk-chapter-pane) { left:52px; max-width:calc(100vw - 52px); }
 .workbench-inner :deep(.desk-reference-pane) { width:calc(100vw - 52px)!important; }
}
</style>
