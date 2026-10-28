<template>
  <n-modal :show="show" preset="card" title="编辑角色档案" class="character-editor-modal"
    style="width: min(880px, 94vw)" :mask-closable="false" :close-on-esc="false" :closable="!busy"
    @update:show="requestClose">
    <div v-if="baseline" class="character-editor">
      <n-alert type="info" :show-icon="false">
        改名会同步本书当前章节和关联资料中的角色名；历史版本与审稿原始证据保留。
        世界观生成或审核进行中时无法保存，请完成或停止相关任务后重试。
      </n-alert>
      <n-alert v-if="error" type="error" :show-icon="false" role="alert">{{ error }}</n-alert>
      <div class="character-editor-status">
        <span>角色编号：{{ baseline.id }}</span>
        <n-tag v-if="dirty" type="warning" size="small">未保存</n-tag>
      </div>
      <n-form :disabled="busy" label-placement="top" class="character-editor-form">
        <n-form-item label="姓名（必填）">
          <n-input v-model:value="draft.name" data-testid="character-name" :disabled="busy" :maxlength="100" />
        </n-form-item>
        <div class="character-editor-grid">
          <n-form-item v-for="field in textFields" :key="field.key" :label="field.label" :class="{ 'character-editor-wide': field.multiline }">
            <n-input v-model:value="draft[field.key]" :data-testid="`character-${field.key}`" :disabled="busy"
              :type="field.multiline ? 'textarea' : 'text'" :autosize="field.multiline ? { minRows: 2, maxRows: 8 } : undefined" />
          </n-form-item>
        </div>
        <n-form-item label="隐藏身份揭示章节（留空表示不限制）">
          <n-input-number :value="draft.reveal_chapter ?? null" :min="1" :precision="0" :disabled="busy"
            @update:value="draft.reveal_chapter = $event" />
        </n-form-item>
        <n-form-item label="行为禁区（每行一条）">
          <n-input :value="(draft.moral_taboos ?? []).join('\n')" type="textarea" :disabled="busy" :autosize="{ minRows: 2, maxRows: 6 }"
            @update:value="draft.moral_taboos = lines($event)" />
        </n-form-item>
        <n-divider>人物关系</n-divider>
        <div v-for="(relationship, index) in draft.relationships" :key="index" class="character-editor-row">
          <n-input v-if="typeof relationship === 'string'" :value="relationship" placeholder="关系描述" :disabled="busy"
            @update:value="draft.relationships[index] = $event" />
          <div v-else class="character-editor-row-fields">
            <n-input v-model:value="relationship.target" aria-label="关系对象" placeholder="关系对象" :disabled="busy" />
            <n-input v-model:value="relationship.relation" aria-label="关系类型" placeholder="关系类型" :disabled="busy" />
            <n-input v-model:value="relationship.description" aria-label="关系描述" placeholder="关系描述" :disabled="busy" />
          </div>
          <n-button size="small" :disabled="busy" @click="draft.relationships.splice(index, 1)">移除关系</n-button>
        </div>
        <n-button size="small" :disabled="busy" @click="draft.relationships.push({ target: '', relation: '', description: '' })">添加关系</n-button>
        <n-divider>声线印记</n-divider>
        <div class="character-editor-grid">
          <n-form-item v-for="field in voiceFields" :key="field.key" :label="field.label">
            <n-input :value="voiceText(field.key)" :disabled="busy" @update:value="setVoiceText(field.key, $event)" />
          </n-form-item>
          <n-form-item v-for="field in voiceListFields" :key="field.key" :label="field.label">
            <n-input :value="voiceListText(field.key)" type="textarea" :disabled="busy" :autosize="{ minRows: 2, maxRows: 5 }"
              @update:value="setVoiceList(field.key, $event)" />
          </n-form-item>
        </div>
        <n-divider>创伤与触发</n-divider>
        <div v-for="(wound, index) in draft.active_wounds ?? []" :key="index" class="character-editor-row">
          <div class="character-editor-row-fields">
            <n-input v-model:value="wound.trigger" aria-label="创伤触发" placeholder="触发情境" :disabled="busy" />
            <n-input v-model:value="wound.effect" aria-label="创伤反应" placeholder="心理或行为反应" :disabled="busy" />
            <n-input v-model:value="wound.description" aria-label="创伤经历" placeholder="经历描述" :disabled="busy" />
          </div>
          <n-button size="small" :disabled="busy" @click="draft.active_wounds?.splice(index, 1)">移除创伤</n-button>
        </div>
        <n-button size="small" :disabled="busy" @click="addWound">添加创伤</n-button>
      </n-form>
    </div>
    <template #footer>
      <div class="character-editor-footer">
        <n-button type="error" secondary :disabled="busy || !baseline" :loading="deleting" @click="confirmDelete">删除角色</n-button>
        <div class="character-editor-footer-actions">
          <n-button :disabled="busy" @click="reloadLatest">读取最新档案</n-button>
          <n-button :disabled="busy" @click="requestClose">取消</n-button>
          <n-button type="primary" :disabled="busy || !dirty || !draft.name?.trim()" :loading="saving" @click="save">保存档案</n-button>
        </div>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { bibleApi, type CharacterDTO } from '@/api/bible'
import { characterProfilesApi, type CharacterMutationResult } from '@/api/characterProfiles'
import { formatApiError, getHttpStatus } from '@/utils/apiError'

const props = defineProps<{ show: boolean; slug: string; character: CharacterDTO | null }>()
const emit = defineEmits<{
  'update:show': [show: boolean]
  saved: [result: CharacterMutationResult, characterId: string]
  deleted: [result: CharacterMutationResult, characterId: string]
}>()
const dialog = useDialog(), message = useMessage()
const baseline = ref<CharacterDTO | null>(null)
const draft = ref<CharacterDTO>({ id: '', name: '', description: '', relationships: [] })
const editingSlug = ref('')
const saving = ref(false), deleting = ref(false), reloading = ref(false), error = ref('')
const busy = computed(() => saving.value || deleting.value || reloading.value)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
type TextFieldKey = 'description' | 'gender' | 'age' | 'appearance' | 'personality' | 'background' | 'core_motivation' | 'inner_lack' |
  'mental_state' | 'mental_state_reason' | 'verbal_tic' | 'idle_behavior' | 'core_belief' | 'public_profile' | 'hidden_profile'
const textFields: Array<{ key: TextFieldKey; label: string; multiline?: boolean }> = [
  { key: 'gender', label: '性别' }, { key: 'age', label: '年龄' },
  { key: 'description', label: '角色简介', multiline: true }, { key: 'appearance', label: '外貌', multiline: true },
  { key: 'personality', label: '性格', multiline: true }, { key: 'background', label: '背景经历', multiline: true },
  { key: 'core_motivation', label: '核心动机', multiline: true }, { key: 'inner_lack', label: '内在缺失', multiline: true },
  { key: 'public_profile', label: '公开人设', multiline: true }, { key: 'hidden_profile', label: '隐藏身份', multiline: true },
  { key: 'core_belief', label: '核心信念', multiline: true }, { key: 'mental_state', label: '当前心理状态' },
  { key: 'mental_state_reason', label: '心理状态原因', multiline: true }, { key: 'verbal_tic', label: '口癖' }, { key: 'idle_behavior', label: '习惯动作' },
]
const voiceFields = [{ key: 'style', label: '说话风格' }, { key: 'sentence_pattern', label: '句式习惯' }, { key: 'speech_tempo', label: '说话节奏' }]
const voiceListFields = [{ key: 'catchphrases', label: '口头禅（每行一条）' }, { key: 'metaphors', label: '常用比喻（每行一条）' }]
const lines = (text: string) => text.split('\n').map(value => value.trim()).filter(Boolean)
const voiceText = (key: string) => String(draft.value.voice_profile?.[key] ?? '')
const voiceListText = (key: string) => {
  const value = draft.value.voice_profile?.[key]
  return Array.isArray(value) ? value.map(String).join('\n') : ''
}
function setVoiceText(key: string, value: string) { draft.value.voice_profile = { ...draft.value.voice_profile, [key]: value } }
function setVoiceList(key: string, value: string) { draft.value.voice_profile = { ...draft.value.voice_profile, [key]: lines(value) } }
function addWound() { (draft.value.active_wounds ??= []).push({ trigger: '', effect: '', description: '' }) }
const changes = computed(() => {
  const result: Record<string, unknown> = {}
  if (!baseline.value) return result
  const before = baseline.value as unknown as Record<string, unknown>
  const after = draft.value as unknown as Record<string, unknown>
  for (const key of Object.keys(after)) {
    if (key !== 'id' && key !== 'role' && JSON.stringify(before[key]) !== JSON.stringify(after[key])) result[key] = after[key]
  }
  return result as Partial<Omit<CharacterDTO, 'id'>>
})
const dirty = computed(() => Object.keys(changes.value).length > 0)
function accept(character: CharacterDTO) {
  baseline.value = clone(character)
  draft.value = clone(character)
  error.value = ''
}
watch(() => props.show, show => {
  if (show && props.character && !busy.value) { editingSlug.value = props.slug; accept(props.character) }
}, { immediate: true })

let pendingLeave: Promise<boolean> | null = null
async function canLeave(): Promise<boolean> {
  if (!props.show) return true
  if (busy.value) { message.warning('角色档案正在保存或读取，请稍候'); return false }
  if (!dirty.value) return true
  if (pendingLeave) return pendingLeave
  pendingLeave = new Promise<boolean>(resolve => {
    dialog.warning({ title: '放弃未保存的角色档案？', content: '本次修改尚未保存，离开会丢失这些修改。',
      positiveText: '放弃修改', negativeText: '继续编辑',
      onPositiveClick: () => { if (baseline.value) accept(baseline.value); resolve(true) },
      onNegativeClick: () => resolve(false), onClose: () => resolve(false), onMaskClick: () => resolve(false),
    })
  })
  try { return await pendingLeave } finally { pendingLeave = null }
}
async function requestClose() { if (await canLeave()) emit('update:show', false) }
function showError(reason: unknown, fallback: string) {
  const detail = formatApiError(reason, fallback)
  error.value = getHttpStatus(reason) === 409 ? `${detail}（409）。本地修改已保留；可读取最新档案后重新编辑。` : detail
}
async function save() {
  if (busy.value || !baseline.value || !dirty.value || !draft.value.name.trim()) return
  saving.value = true; error.value = ''
  const expected = clone(baseline.value), payload = clone(changes.value), novelId = editingSlug.value
  try {
    const result = await characterProfilesApi.update(novelId, expected.id, payload, expected)
    const saved = result.characters.find(character => character.id === expected.id)
    if (!saved) throw new Error('保存结果缺少角色档案，请读取最新档案核实')
    accept(saved)
    emit('saved', result, expected.id)
    emit('update:show', false)
    const savedText = payload.name && payload.name !== expected.name ? '角色档案已保存，名称已同步到本书当前章节和关联资料' : '角色档案已保存'
    message.success(result.character_mutation?.derivation_jobs
      ? `${savedText}；关联分析已提交，可在章节历史查看进度，失败后可重跑`
      : savedText)
  } catch (reason) { showError(reason, '保存角色档案失败，请重试') }
  finally { saving.value = false }
}
function confirmDelete() {
  if (busy.value || !baseline.value) return
  const expected = clone(baseline.value), novelId = editingSlug.value
  dialog.warning({ title: `删除角色「${expected.name}」？`,
    content: '将移除角色档案、出场配置及关联的角色记忆和关系；历史章节正文、历史版本和审稿原始证据会保留。此操作也会放弃本次未保存的编辑。',
    positiveText: '确认删除角色', negativeText: '保留角色',
    onPositiveClick: async () => {
      if (busy.value) return false
      deleting.value = true; error.value = ''
      try {
        const result = await characterProfilesApi.delete(novelId, expected.id, expected)
        baseline.value = null
        emit('deleted', result, expected.id)
        emit('update:show', false)
        message.success('角色已删除，历史章节正文已保留')
      } catch (reason) { showError(reason, '删除角色失败，请重试'); return false }
      finally { deleting.value = false }
    },
  })
}
async function reloadLatest() {
  if (!baseline.value || !await canLeave()) return
  reloading.value = true
  try {
    const result = await bibleApi.getBible(editingSlug.value)
    const character = result.characters.find(item => item.id === baseline.value?.id)
    if (!character) throw new Error('此角色已不存在，请关闭档案并刷新角色列表')
    accept(character)
  } catch (reason) { showError(reason, '读取最新档案失败') }
  finally { reloading.value = false }
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (props.show && (dirty.value || busy.value)) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
defineExpose({ canLeave })
</script>

<style scoped>
.character-editor { max-height: 68vh; overflow-y: auto; padding-right: 8px; }
.character-editor > :deep(.n-alert) { margin-bottom: 12px; }
.character-editor-status { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--app-text-muted); margin-bottom: 12px; }
.character-editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.character-editor-wide { grid-column: 1 / -1; }
.character-editor-row { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 10px; }
.character-editor-row-fields { display: grid; flex: 1; min-width: 0; gap: 6px; }
.character-editor-footer, .character-editor-footer-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.character-editor-footer { justify-content: space-between; }
@media (max-width: 600px) { .character-editor-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
