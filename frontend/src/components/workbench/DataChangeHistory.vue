<template>
  <section>
    <n-button size="small" :loading="loading" @click="load">刷新变更与恢复记录</n-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-empty v-if="!items.length && !loading" description="暂无结构变更记录" />
    <details v-for="item in items" :key="item.proposal_id" class="change-record">
      <summary>{{ operationLabel(item.operation) }} · {{ item.created_at }} · {{ stateLabel(item) }}</summary>
      <p>保护快照 {{ item.snapshot_id }}</p>
      <p>受影响章节 {{ item.proposal.affected_chapters.length }} 个；有正文 {{ item.proposal.affected_chapters.filter(chapter => chapter.has_content).length }} 个。</p>
      <p v-for="chapter in item.proposal.affected_chapters" :key="chapter.id">第 {{ chapter.number }} 章 {{ chapter.title }} · 原版本 {{ chapter.revision }}</p>
      <p v-for="audit in item.audits" :key="audit.audit_id">{{ audit.event }} · 审计 {{ audit.audit_id }}</p>
      <n-button v-if="item.status === 'applied'" :disabled="busy" @click="restore(item)">恢复此次变更前的数据</n-button>
      <p v-if="item.status === 'applied'">恢复前会检查当前数据是否仍匹配这次变更；有后续编辑时将拒绝覆盖。</p>
    </details>
  </section>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import { NButton, NEmpty, useDialog, useMessage } from 'naive-ui'
import { dataProtectionApi, type DataChangeProposal } from '../../api/dataProtection'
const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ restored: [] }>()
const dialog = useDialog(), message = useMessage()
const items = ref<DataChangeProposal[]>([]), loading = ref(false), busy = ref(false), error = ref('')
let generation = 0
const operationLabel = (operation: string) => ({ delete_nodes: '结构删除', snapshot_rollback: '章节集合回退', macro_merge: '宏观结构合并' } as Record<string, string>)[operation] || operation
const stateLabel = (item: DataChangeProposal) => item.expired ? '提案已过期' : ({ pending: '待确认', applied: '已应用 · 可恢复', rolled_back: '已恢复' } as Record<string, string>)[item.status] || item.status
async function load() {
  const id = ++generation, novelId = props.novelId
  loading.value = true; error.value = ''
  try { const data = await dataProtectionApi.list(novelId); if (id === generation && novelId === props.novelId) items.value = data }
  catch (exc) { if (id === generation) error.value = exc instanceof Error ? exc.message : '变更记录加载失败' }
  finally { if (id === generation) loading.value = false }
}
function restore(item: DataChangeProposal) {
  const novelId = props.novelId
  dialog.warning({ title: '恢复此次变更前的数据？', content: `将从保护快照恢复「${operationLabel(item.operation)}」前的数据。版本冲突时会中止。`,
    positiveText: '检查并恢复', negativeText: '取消', onPositiveClick: async () => {
      if (novelId !== props.novelId || busy.value) return false
      busy.value = true
      try { await dataProtectionApi.rollback(novelId, item); message.success('数据已恢复，恢复审计已记录'); emit('restored'); await load() }
      catch (exc) { message.error(exc instanceof Error ? exc.message : '恢复失败，请刷新变更记录'); return false }
      finally { busy.value = false }
    } })
}
watch(() => props.novelId, () => { items.value = []; void load() }, { immediate: true })
</script>
<style scoped>.change-record { padding: 12px; margin: 10px 0; border: 1px solid var(--border-color, #d1d5db); border-radius: 6px; overflow-wrap: anywhere; }</style>
