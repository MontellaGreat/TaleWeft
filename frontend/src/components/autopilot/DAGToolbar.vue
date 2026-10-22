<template>
  <div class="dag-toolbar">
    <div class="toolbar-left">
      <n-text strong class="toolbar-title-text">🧭 DAG 可视化</n-text>

      <!-- 节点统计（精简） -->
      <n-tag v-if="dagStats" size="small" round>
        {{ dagStats.total }} 节点 · {{ dagStats.enabled }} 启用
        <template v-if="dagStats.running > 0">
          · <n-text type="info">{{ dagStats.running }} 运行中</n-text>
        </template>
        <template v-if="dagStats.error > 0">
          · <n-text type="error">{{ dagStats.error }} 错误</n-text>
        </template>
      </n-tag>

      <!-- ★ 托管模式状态指示 -->
      <n-tag
        v-if="autopilotStatus === 'running'"
        size="small"
        type="info"
        round
        :bordered="false"
      >
        <template #icon>
          <n-spin :size="12" />
        </template>
        托管运行中
      </n-tag>
      <n-tag
        v-else-if="autopilotStatus === 'paused'"
        size="small"
        type="warning"
        round
        :bordered="false"
      >
        ⏸️ 等待审阅
      </n-tag>
      <n-tag
        v-else-if="autopilotStatus === 'completed'"
        size="small"
        type="success"
        round
        :bordered="false"
      >
        ✅ 全书完成
      </n-tag>
      <n-tag
        v-else-if="autopilotStatus === 'error'"
        size="small"
        type="error"
        round
        :bordered="false"
      >
        ❌ 托管异常
      </n-tag>

      <!-- SSE 连接状态 -->
      <n-tooltip trigger="hover">
        <template #trigger>
          <div class="sse-indicator" :class="{ connected: sseConnected }" />
        </template>
        {{ sseConnected ? 'SSE 实时连接正常' : 'SSE 连接断开（托管未运行时不会自动重连）' }}
      </n-tooltip>

      <n-tooltip v-if="registryGapCount > 0" trigger="hover">
        <template #trigger>
          <n-tag size="small" type="error" round>缺注册 {{ registryGapCount }}</n-tag>
        </template>
        画布上有节点类型未在引擎注册表中找到，详见上方提示条。
      </n-tooltip>
      <n-tooltip v-else-if="linkageFailed" trigger="hover">
        <template #trigger>
          <n-tag size="small" type="warning" round>联动</n-tag>
        </template>
        注册表联动接口未加载完成，广场映射可能不完整。
      </n-tooltip>
    </div>

    <div class="toolbar-right">
      <n-tag v-if="runStore.executionMode === 'dag_runner'" size="small" :type="runStore.runStatus === 'error' ? 'error' : 'info'">
        第 {{ runStore.chapterNumber || '—' }} 章 · {{ dagStatusLabel }}
      </n-tag>
      <n-input-number v-model:value="selectedChapter" :min="1" :precision="0" placeholder="下一草稿章" size="small" style="width: 128px" :disabled="runStore.isRunning" />
      <n-button size="small" type="primary" :disabled="!runStore.canStart || autopilotStatus === 'running'" @click="startChapter">运行本章 DAG</n-button>
      <n-button size="small" :disabled="!runStore.canStop" :loading="runStore.runStatus === 'stopping'" @click="stopChapter">停止</n-button>
      <n-button v-if="runStore.currentRunId && ['error', 'cancelled'].includes(runStore.runStatus)" size="small" :disabled="!runStore.canStart" @click="resumeCandidate">恢复候选并复验</n-button>
      <!-- 版本信息 -->
      <n-text depth="3" class="toolbar-version" v-if="dagStats">
        v{{ dagStats.version || 1 }}
      </n-text>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { useDAGStore } from '@/stores/dagStore'
import { useDAGRunStore } from '@/stores/dagRunStore'

const dagStore = useDAGStore()
const runStore = useDAGRunStore()
const message = useMessage()
const selectedChapter = ref<number | null>(null)
const dagStatusLabel = computed(() => ({ idle: '待运行', running: '运行中', stopping: '等待退出', completed: '执行完成', awaiting_review: '等待审阅', cancelled: '已取消', error: '执行失败' }[runStore.runStatus]))

async function startChapter() {
  try {
    dagStore.resetNodeStates()
    await runStore.startRun(props.novelId, selectedChapter.value ? { chapter_number: selectedChapter.value } : {})
  } catch (error) {
    message.error(error instanceof Error ? error.message : '启动失败')
  }
}

async function stopChapter() {
  try {
    await runStore.stopRun(props.novelId)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '停止未确认')
  }
}

async function resumeCandidate() {
  if (!runStore.currentRunId) return
  try {
    await runStore.startRun(props.novelId, { chapter_number: runStore.chapterNumber || undefined, resume_run_id: runStore.currentRunId })
  } catch (error) {
    message.error(error instanceof Error ? error.message : '候选恢复失败')
  }
}

const registryGapCount = computed(() => dagStore.registryGaps.length)
const linkageFailed = computed(() => dagStore.registryLinkageFailed)

const props = defineProps<{
  novelId: string
  dagStats: {
    total: number
    enabled: number
    running: number
    success: number
    error: number
    bypassed: number
    version?: number
  }
  /** ★ 托管模式状态 */
  autopilotStatus: 'idle' | 'running' | 'paused' | 'completed' | 'error'
  sseConnected: boolean
}>()

defineEmits<{
  'switch-to-card': []
}>()
</script>

<style scoped>
.dag-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 16px;
  border-bottom: 1px solid var(--dag-toolbar-border);
  background: var(--dag-toolbar-bg);
  gap: 12px;
  min-height: 40px;
  flex-wrap: wrap;
  row-gap: 8px;
  flex-shrink: 0;
  position: relative;
  z-index: 20;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-title-text {
  font-size: 14px;
  color: var(--app-text-primary);
}

.toolbar-version {
  font-size: 11px;
}

/* ── SSE 连接指示灯 ── */
.sse-indicator {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-danger);
  transition: background 0.3s;
  flex-shrink: 0;
}

.sse-indicator.connected {
  background: var(--color-success);
  animation: dag-pulse 2s ease-in-out infinite;
}

@keyframes dag-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
</style>
