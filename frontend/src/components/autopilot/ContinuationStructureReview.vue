<template>
  <section class="continuation-review" aria-label="续写结构审阅">
    <header><strong>{{ stateLabel }}</strong><n-button size="small" :disabled="busy" @click="emit('refresh')">刷新续写状态</n-button></header>
    <p>第 {{ proposal.from_chapter }} 章起续写 · 目标 {{ proposal.base_target_chapters }} → {{ proposal.proposed_target_chapters }} 章。</p>
    <n-alert v-if="proposal.status === 'legacy_unverified'" type="warning" :show-icon="false">旧版记录缺少结构确认凭据，不能据此判断续写已完成。请重新提出续写提案。</n-alert>
    <template v-else-if="proposal.status === 'awaiting_structure_review'">
      <p>请核对部、卷、幕及变更范围。确认后才应用结构并进入幕级规划。</p>
      <div class="continuation-tree">
        <details v-for="(part, pi) in proposal.structure || []" :key="pi" open>
          <summary>部 {{ pi + 1 }} · {{ part.title }}</summary><p v-if="part.description">{{ part.description }}</p>
          <details v-for="(volume, vi) in part.volumes || []" :key="vi" open>
            <summary>卷 {{ vi + 1 }} · {{ volume.title }}</summary><p v-if="volume.description">{{ volume.description }}</p>
            <div v-for="(act, ai) in volume.acts || []" :key="ai" class="continuation-act"><strong>幕 {{ ai + 1 }} · {{ act.title }}</strong><p v-if="act.description">{{ act.description }}</p></div>
          </details>
        </details>
      </div>
      <template v-if="review">
        <p>新增 {{ review.proposal.creates?.length || 0 }} 个节点 · 更新 {{ review.proposal.updates?.length || 0 }} 个节点 · 删除 {{ review.proposal.deletes?.length || 0 }} 个空节点。</p>
        <p>涉及已有章节 {{ review.proposal.affected_chapters.length }} 个；有正文 {{ review.proposal.affected_chapters.filter(ch => ch.has_content).length }} 个。</p>
        <details v-if="review.proposal.affected_chapters.length"><summary>查看涉及的已有章节</summary><p v-for="chapter in review.proposal.affected_chapters" :key="chapter.id">第 {{ chapter.number }} 章 {{ chapter.title }} · 版本 {{ chapter.revision }}</p></details>
      </template>
      <n-alert v-if="!canConfirm" type="warning" :show-icon="false">结构审阅已过期或版本已变化，请放弃此提案并重新规划。</n-alert>
      <n-button v-if="canConfirm" type="primary" :disabled="busy" @click="openConfirm">确认采用续写结构</n-button>
    </template>
    <p v-else-if="proposal.status === 'approved' || proposal.status === 'macro_planning'">{{ proposal.status === 'approved' ? '提案已采纳，等待执行器开始宏观规划。' : '正在生成续写结构，生成后仍需作者明确确认。' }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-button v-if="canReject" type="error" secondary :disabled="busy" @click="showReject = true">放弃此次续写</n-button>
    <n-modal v-model:show="showConfirm" preset="card" title="应用已审阅的续写结构？" style="width: min(520px, calc(100vw - 32px))">
      <p>将核验您正在审阅的结构、正文和运行版本，在同一事务中应用结构并进入幕级规划。已有正文会保留；版本冲突会中止。</p>
      <n-space justify="end"><n-button :disabled="busy" @click="showConfirm = false">取消</n-button><n-button type="primary" :loading="busy" @click="confirm">核验版本并应用结构</n-button></n-space>
    </n-modal>
    <n-modal v-model:show="showReject" preset="card" title="放弃此次续写？" style="width: min(520px, calc(100vw - 32px))">
      <p>未应用的结构不会写入作品。作品仍处于此次续写版本且未产生新正文时，将恢复原完稿目标。</p>
      <n-space justify="end"><n-button :disabled="busy" @click="showReject = false">取消</n-button><n-button type="error" :loading="busy" @click="reject">确定放弃续写</n-button></n-space>
    </n-modal>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NSpace, useMessage } from 'naive-ui'
import { autopilotApi, getAutopilotErrorDetail, type ContinuationProposal } from '../../api/autopilot'
const props = defineProps<{ novelId: string; proposal: ContinuationProposal }>()
const emit = defineEmits<{ refresh: []; changed: [] }>()
const message = useMessage()
const busy = ref(false), error = ref(''), showConfirm = ref(false), showReject = ref(false)
const displayed = ref<ContinuationProposal | null>(null)
const review = computed(() => props.proposal.structure_review)
const canConfirm = computed(() => props.proposal.status === 'awaiting_structure_review' && Boolean(
  props.proposal.structure?.length && review.value && review.value.status === 'pending' && !review.value.expired &&
  review.value.run_epoch === review.value.current_run_epoch && review.value.run_epoch === props.proposal.run_epoch &&
  review.value.proposal_id === props.proposal.structure_change_id,
))
const canReject = computed(() => ['approved', 'macro_planning', 'awaiting_structure_review'].includes(props.proposal.status))
const stateLabel = computed(() => ({ approved: '续写提案已采纳', macro_planning: '续写宏观规划中', awaiting_structure_review: '待作者审阅续写结构', legacy_unverified: '旧版续写状态待核验' } as Record<string, string>)[props.proposal.status] || props.proposal.status)
function openConfirm() {
  if (!canConfirm.value || busy.value) return
  displayed.value = JSON.parse(JSON.stringify(props.proposal)) as ContinuationProposal
  showConfirm.value = true
}
function stillDisplayed(proposal: ContinuationProposal) {
  return props.novelId === proposal.novel_id && props.proposal.proposal_id === proposal.proposal_id &&
    props.proposal.structure_review?.proposal_hash === proposal.structure_review?.proposal_hash &&
    props.proposal.structure_review?.base_hash === proposal.structure_review?.base_hash && canConfirm.value
}
async function confirm() {
  const proposal = displayed.value
  if (busy.value || !proposal || !stillDisplayed(proposal)) { showConfirm.value = false; return }
  busy.value = true; error.value = ''
  try {
    const receipt = await autopilotApi.confirmContinuationStructure(props.novelId, proposal)
    if (props.novelId !== proposal.novel_id) return
    if (receipt.status !== 'applied' || receipt.proposal_id !== proposal.structure_change_id) throw new Error('结构应用结果尚未确认，请刷新续写状态')
    showConfirm.value = false; message.success('续写结构已应用，进入幕级规划'); emit('changed')
  } catch (exc) { if (props.novelId === proposal.novel_id) error.value = getAutopilotErrorDetail(exc) || (exc instanceof Error ? exc.message : '结构确认失败，请刷新后核对'); emit('refresh') }
  finally { busy.value = false }
}
async function reject() {
  if (busy.value || !canReject.value) return
  const proposal = props.proposal, novelId = props.novelId
  busy.value = true; error.value = ''
  try { await autopilotApi.rejectContinuationProposal(novelId, proposal.proposal_id); if (novelId === props.novelId) { showReject.value = false; message.info('此次续写已放弃'); emit('changed') } }
  catch (exc) { if (novelId === props.novelId) error.value = getAutopilotErrorDetail(exc) || (exc instanceof Error ? exc.message : '放弃失败，请刷新后核对'); emit('refresh') }
  finally { busy.value = false }
}
watch(() => [props.novelId, props.proposal.proposal_id], () => { showConfirm.value = false; showReject.value = false; displayed.value = null; error.value = '' })
</script>
<style scoped>
.continuation-review { border: 1px solid var(--border-color, #d1d5db); border-radius: 8px; padding: 16px; margin: 12px 0; overflow-wrap: anywhere; }
header { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.continuation-tree { max-height: 420px; overflow: auto; margin: 12px 0; }
details { padding: 8px 0 8px 14px; border-left: 2px solid var(--border-color, #d1d5db); }
summary { cursor: pointer; font-weight: 600; }
.continuation-act { padding: 10px 14px; }
p { margin: 8px 0; white-space: pre-wrap; }
</style>
