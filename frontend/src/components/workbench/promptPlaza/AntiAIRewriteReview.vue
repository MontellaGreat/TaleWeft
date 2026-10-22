<template>
  <section class="rewrite-review">
    <n-alert type="info" :show-icon="false">
      仅扫描会保留检测结果；作者确认会生成并复验候选；自动提交只采用通过规则、质量和声线复验的改稿。确认或拒绝不会自动恢复生成。
    </n-alert>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-spin :show="loading">
      <template v-if="policy">
        <div class="policy-form">
          <label>处理策略<n-select v-model:value="policy.mode" :options="modes" :disabled="busy" /></label>
          <label>最多改写次数<n-input-number v-model:value="policy.max_attempts" :min="1" :max="5" :disabled="busy" /></label>
          <label>规则评分上限<n-input-number v-model:value="policy.max_severity" :min="0" :max="100" :disabled="busy" /></label>
          <label>写作场景规则<n-select v-model:value="policy.scene_type" :options="sceneTypes" :disabled="busy" /></label>
          <label>流式严重命中<n-select v-model:value="policy.stream_action" :options="streamActions" :disabled="busy" /></label>
          <label v-if="policy.stream_action === 'cancel'">停止阈值<n-input-number v-model:value="policy.stream_critical_limit" :min="1" :max="100" :disabled="busy" /></label>
          <n-button :loading="busy" @click="savePolicy">保存策略</n-button>
        </div>
      </template>
      <n-button size="small" :disabled="busy || !novelId" @click="reload">刷新候选与状态</n-button>
      <n-empty v-if="!candidates.length" description="暂无改写候选；选择作者确认或自动策略后，章节审计达到阈值时会生成候选" />
      <article v-for="candidate in candidates" :key="candidate.candidate_id" class="candidate-card">
        <h4>第 {{ candidate.chapter_number }} 章 · 第 {{ candidate.attempt }} 次改写
          <n-tag :type="candidate.stale ? 'warning' : finalAuditComplete(candidate) && aftermathComplete(candidate) ? 'success' : 'default'">{{ statusLabel(candidate) }}</n-tag>
        </h4>
        <p>生成时间 {{ candidate.created_at }} · 原稿版本 {{ candidate.base_revision }} · 候选 {{ candidate.content_hash.slice(0, 12) }}</p>
        <n-alert v-if="candidate.stale" type="warning" :show-icon="false">原稿或运行已经变化，旧候选不能覆盖当前正文。可查看差异并手工合并，或根据当前正文重新生成。</n-alert>
        <n-alert v-if="candidate.is_generating" type="info" :show-icon="false">正在生成，请暂停后再处理候选。</n-alert>
        <p v-if="candidate.error">{{ candidate.error }}</p>
        <div class="checks">
          <p>规则扫描：{{ candidate.audit.scan?.scan_status === 'completed' ? `已完成 · 评分 ${candidate.audit.scan.metrics.severity_score} · 严重命中 ${candidate.audit.scan.metrics.critical_hits}` : '未完成' }}</p>
          <p>质量复验：{{ candidate.audit.quality?.passed ? '通过' : '未通过或不可用' }} {{ candidate.audit.quality?.error }}</p>
          <p>声线复验：{{ candidate.audit.voice?.status === 'completed' ? `已完成 · 相似度 ${candidate.audit.voice.similarity_score}` : '未完成或不可用' }} {{ candidate.audit.voice?.error }}</p>
          <p v-for="reason in candidate.audit.reasons" :key="reason">{{ reason }}</p>
          <p v-if="candidate.status === 'committed'">正文已提交为版本 {{ candidate.audit.committed_revision }}；最终审计{{ finalAuditComplete(candidate) ? '已归档' : '尚未确认，可重新核验归档' }}。</p>
          <p v-if="candidate.audit.aftermath">章后派生：{{ aftermathComplete(candidate) ? '已完成' : '未完成，可重新核验后重试' }} {{ candidate.audit.aftermath.error }}</p>
        </div>
        <details><summary>查看原稿与候选差异</summary><pre class="diff">{{ candidate.diff || '没有正文变化' }}</pre></details>
        <details><summary>查看完整原稿与候选</summary><div class="bodies"><pre>{{ candidate.base_content }}</pre><pre>{{ candidate.content }}</pre></div></details>
        <div class="candidate-actions">
          <n-button v-if="candidate.can_confirm" type="primary" :disabled="busy" @click="reviewTarget = candidate">确认采用此候选</n-button>
          <n-button v-if="candidate.status === 'committed' && !candidate.stale && (!finalAuditComplete(candidate) || !aftermathComplete(candidate)) && !candidate.is_generating" :disabled="busy" @click="act(candidate, 'confirm')">重新核验最终审计</n-button>
          <n-button v-if="candidate.status !== 'committed' && candidate.status !== 'rejected'" :disabled="busy || candidate.is_generating" @click="act(candidate, 'reject')">拒绝并保留原稿</n-button>
          <n-button :disabled="busy || candidate.is_generating || candidate.current_revision === null" @click="act(candidate, 'regenerate')">根据当前正文重新生成</n-button>
        </div>
      </article>
    </n-spin>
    <n-modal :show="Boolean(reviewTarget)" preset="dialog" title="确认采用改写候选" positive-text="复验并提交" negative-text="继续检查"
      :loading="busy" @positive-click="confirmSelected" @negative-click="reviewTarget = null" @close="reviewTarget = null">
      你正在采用第 {{ reviewTarget?.chapter_number }} 章的候选正文。系统会重新复验，并检查原稿版本和运行状态；通过后保留原稿历史，再提交正文和最终审计。
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NAlert, NButton, NEmpty, NInputNumber, NModal, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { getRewritePolicy, saveRewritePolicy, getRewriteCandidates, confirmRewriteCandidate, rejectRewriteCandidate, regenerateRewriteCandidate } from '../../../api/anti-ai'
import type { RewriteCandidate, AntiAIRewritePolicy } from '../../../types/anti-ai'

const props = defineProps<{ novelId: string | null }>()
const message = useMessage()
const policy = ref<AntiAIRewritePolicy | null>(null)
const candidates = ref<RewriteCandidate[]>([])
const reviewTarget = ref<RewriteCandidate | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
let generation = 0
const modes = [{ label: '仅扫描', value: 'scan_only' }, { label: '作者确认', value: 'review' }, { label: '自动提交通过复验的改稿', value: 'auto' }]
const streamActions = [{ label: '告警并保留片段（默认）', value: 'warn' }, { label: '达到严重命中阈值时停止本次生成', value: 'cancel' }]
const sceneTypes = [{label:'默认',value:'default'},{label:'战斗',value:'battle'},{label:'悬疑',value:'suspense'},
  {label:'恐怖',value:'horror'},{label:'告白',value:'confession'},{label:'揭秘/反转',value:'revelation'}]

function aftermathComplete(candidate: RewriteCandidate) {
  const result = candidate.audit.aftermath
  return !result || result.narrative_sync_ok === true && !['failed', 'superseded'].includes(result.derivation_status || '')
}

function finalAuditComplete(candidate: RewriteCandidate) {
  const result = candidate.audit.final_audit
  return candidate.status === 'committed' && result?.scan_status === 'completed'
    && result.persistence_status === 'committed' && Boolean(result.run_id)
    && result.content_hash === candidate.content_hash
}

function statusLabel(candidate: RewriteCandidate) {
  if (candidate.stale) return '版本已过期'
  if (candidate.status === 'committed') return !aftermathComplete(candidate) ? '正文已提交 · 章后派生待重试' : finalAuditComplete(candidate) ? '已提交并归档' : '正文已提交 · 最终审计待核验'
  return ({ generated: '已生成 · 待复验', audited: '已复验 · 待确认', pending_review: '等待作者确认', rejected: '已拒绝', failed: '操作失败', conflict: '版本冲突', cancelled: '已取消' } as Record<string, string>)[candidate.status] || candidate.status
}

async function reload() {
  const id = ++generation
  const novelId = props.novelId
  candidates.value = []
  policy.value = null
  reviewTarget.value = null
  error.value = ''
  if (!novelId) { loading.value = false; return }
  loading.value = true
  const results = await Promise.allSettled([getRewritePolicy(novelId), getRewriteCandidates(novelId)])
  if (id !== generation || novelId !== props.novelId) return
  if (results[0].status === 'fulfilled') policy.value = results[0].value
  if (results[1].status === 'fulfilled') candidates.value = results[1].value
  if (results.some(result => result.status === 'rejected')) error.value = '部分候选或策略加载失败，请刷新。'
  loading.value = false
}

async function savePolicy() {
  if (!props.novelId || !policy.value || busy.value) return
  const novelId = props.novelId
  busy.value = true
  try {
    const saved = await saveRewritePolicy(novelId, { ...policy.value })
    if (novelId === props.novelId) { policy.value = saved; message.success('策略已保存') }
  } catch (exc) {
    if (novelId === props.novelId) message.error(exc instanceof Error ? exc.message : '策略保存失败')
  } finally { busy.value = false }
}

async function act(candidate: RewriteCandidate, action: 'confirm' | 'reject' | 'regenerate') {
  if (!props.novelId || candidate.novel_id !== props.novelId || busy.value) return false
  const novelId = props.novelId
  busy.value = true
  try {
    if (action === 'confirm') {
      const result = await confirmRewriteCandidate(novelId, candidate.candidate_id, { expected_candidate_hash: candidate.content_hash,
        expected_base_hash: candidate.base_hash, expected_base_revision: candidate.base_revision, expected_run_epoch: candidate.run_epoch })
      if (novelId === props.novelId) {
        if (result.status === 'committed' && result.candidate?.candidate_id === candidate.candidate_id
          && result.candidate.content_hash === candidate.content_hash && finalAuditComplete(result.candidate)
          && aftermathComplete(result.candidate)) {
          message.success('正文已提交，最终审计已归档；可返回写作台决定何时恢复生成')
        } else {
          message.warning('提交或最终审计尚未确认完成，请刷新候选记录并重新核验')
          return false
        }
      }
    } else if (action === 'reject') {
      await rejectRewriteCandidate(novelId, candidate.candidate_id, candidate.content_hash)
      if (novelId === props.novelId) message.info('候选已拒绝，原稿保留；可编辑后重新生成或返回写作台继续审阅')
    } else {
      const result = await regenerateRewriteCandidate(novelId, candidate)
      if (novelId === props.novelId) {
        if (result.status === 'awaiting_confirmation') message.info('新候选已保存，请检查差异并确认')
        else message.warning(result.error || '改写未通过复验，请检查新候选记录')
      }
    }
    return true
  } catch (exc) {
    if (novelId === props.novelId) message.error(exc instanceof Error ? exc.message : '操作未确认完成，请刷新候选记录')
    return false
  } finally {
    busy.value = false
    if (novelId === props.novelId) await reload()
  }
}

async function confirmSelected() {
  const candidate = reviewTarget.value
  if (!candidate) return false
  return await act(candidate, 'confirm')
}

watch(() => props.novelId, () => { void reload() }, { immediate: true })
</script>

<style scoped>
.rewrite-review { display: grid; gap: 16px; }
.policy-form { display: flex; align-items: end; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
.policy-form label { min-width: 160px; display: grid; gap: 6px; }
.candidate-card { padding: 16px; margin-top: 12px; border: 1px solid var(--border-color, #d1d5db); border-radius: 8px; }
.candidate-card h4 { display: flex; gap: 12px; flex-wrap: wrap; }
.candidate-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.checks { font-size: 13px; }
.diff, .bodies pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 400px; overflow: auto; padding: 12px; background: var(--hover-color, #f1f5f9); }
.bodies { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
</style>
