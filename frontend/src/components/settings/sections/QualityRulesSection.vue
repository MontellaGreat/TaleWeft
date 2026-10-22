<template>
  <n-space vertical :size="18">
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <n-alert type="info">扫描规则用于提示常见文风问题。保存会创建新版本；恢复旧版本后，已有审计仍保留原来的规则快照。规则按当前项目保存。</n-alert>
    <n-space align="center" justify="space-between">
      <n-text>当前版本：{{ state?.current.note || '读取中' }}</n-text>
      <n-button :disabled="busy" @click="load">刷新规则</n-button>
    </n-space>
    <n-form label-placement="top">
      <n-form-item label="停用的内置规则">
        <n-select v-model:value="disabled" multiple filterable :options="builtinOptions" :disabled="busy" placeholder="选择需要停用的规则" />
      </n-form-item>
      <n-form-item label="自定义规则">
        <n-space vertical style="width: 100%">
          <n-text depth="3">匹配完整字面文本。严重程度和替换建议会出现在扫描结果中。</n-text>
          <div v-for="(rule, index) in rules" :key="index" class="rule-row">
            <n-input v-model:value="rule.name" placeholder="规则名称" :maxlength="80" :disabled="busy" :aria-label="`规则${index + 1}名称`" />
            <n-input v-model:value="rule.text" placeholder="匹配文本" :maxlength="200" :disabled="busy" :aria-label="`规则${index + 1}匹配文本`" />
            <n-select v-model:value="rule.severity" :options="severities" :disabled="busy" :aria-label="`规则${index + 1}严重程度`" />
            <n-input v-model:value="rule.replacement_hint" placeholder="替换建议（可选）" :maxlength="500" :disabled="busy" :aria-label="`规则${index + 1}替换建议`" />
            <n-button :disabled="busy" @click="rules.splice(index, 1)">删除规则</n-button>
          </div>
          <n-button :disabled="busy || rules.length >= 100" @click="addRule">添加规则</n-button>
        </n-space>
      </n-form-item>
      <n-form-item label="版本说明"><n-input v-model:value="note" :disabled="busy" :maxlength="500" placeholder="说明这次文风规则调整" aria-label="版本说明" /></n-form-item>
    </n-form>
    <n-button type="primary" :loading="busy" :disabled="!state || !note.trim()" @click="save">保存新规则版本</n-button>
    <n-divider>版本历史</n-divider>
    <n-space vertical>
      <n-select v-model:value="restoreVersion" :options="versionOptions" :virtual-scroll="false" :disabled="busy" placeholder="选择要恢复的版本" aria-label="要恢复的规则版本" />
      <n-button :disabled="busy || !restoreVersion || restoreVersion === state?.current.id" @click="restore">恢复此版本</n-button>
    </n-space>
    <n-divider>试扫当前规则</n-divider>
    <n-input v-model:value="sample" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" :maxlength="100000" placeholder="输入一段正文，试扫已保存的规则" aria-label="试扫正文" />
    <n-button :disabled="busy || !sample.trim()" @click="scan">试扫当前规则</n-button>
    <template v-if="result">
      <n-text>命中 {{ result.metrics.total_hits }} 处，严重 {{ result.metrics.critical_hits }} 处，评分 {{ result.metrics.severity_score }}：{{ result.metrics.overall_assessment }}</n-text>
      <n-text v-for="(hit, index) in result.hits" :key="index">{{ hit.pattern }}：{{ hit.text }} {{ hit.replacement_hint }}</n-text>
      <n-text depth="3" class="digest">扫描规则 {{ result.rule_version }}</n-text>
    </template>
  </n-space>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NDivider, NForm, NFormItem, NInput, NSelect, NSpace, NText, useDialog, useMessage } from 'naive-ui'
import { extensionPolicyApi, extensionError, type QualityRulesOverview, type LiteralQualityRule, type QualityScanPreview } from '@/api/extensionPolicy'
const state = ref<QualityRulesOverview | null>(null)
const disabled = ref<string[]>([]), rules = ref<LiteralQualityRule[]>([]), note = ref(''), restoreVersion = ref<string | null>(null), sample = ref('')
const result = ref<QualityScanPreview | null>(null), busy = ref(false), error = ref('')
const dialog = useDialog(), message = useMessage()
const severities = [{ label: '严重', value: 'critical' }, { label: '提醒', value: 'warning' }, { label: '提示', value: 'info' }]
const builtinOptions = computed(() => state.value?.builtin.map(rule => ({ label: `${rule.category}：${rule.name}`, value: rule.name })) ?? [])
const versionOptions = computed(() => state.value?.versions.map(version => ({ label: `${version.note} (${version.created_at})${version.id === state.value?.current.id ? ' 当前' : ''}`, value: version.id })) ?? [])
async function run(action: () => Promise<void>) { busy.value = true; error.value = ''; try { await action() } catch (e) { error.value = extensionError(e) } finally { busy.value = false } }
async function refresh() { const loaded = await extensionPolicyApi.rules(); state.value = loaded; disabled.value = [...loaded.current.config.disabled_builtin]; rules.value = loaded.current.config.rules.map(rule => ({ ...rule })); note.value = '' }
async function load() { await run(refresh) }
function addRule() { rules.value.push({ name: '', text: '', severity: 'warning', category: '自定义', replacement_hint: '' }) }
async function save() { const current = state.value; if (!current) return; await run(async () => { await extensionPolicyApi.publish({ disabled_builtin: [...disabled.value], rules: rules.value.map(rule => ({ ...rule })) }, current.current.id, note.value); await refresh(); message.success('新规则版本已保存') }) }
function restore() {
  const target = restoreVersion.value, current = state.value?.current.id
  if (!target || !current) return
  dialog.warning({ title: '恢复规则版本', content: '恢复会创建一个新版本，后续扫描使用恢复后的规则。已有扫描记录保留。', positiveText: '确认恢复规则', negativeText: '取消', onPositiveClick: async () => { await run(async () => { await extensionPolicyApi.rollback(target, current); await refresh(); message.success('规则版本已恢复') }) } })
}
async function scan() { await run(async () => { result.value = await extensionPolicyApi.scan(sample.value) }) }
onMounted(load)
</script>

<style scoped>
.rule-row { display: grid; grid-template-columns: 1fr 1.5fr 90px 1.5fr auto; gap: 8px; align-items: start; }
.digest { overflow-wrap: anywhere; }
@media (max-width: 800px) { .rule-row { grid-template-columns: 1fr 1fr; } }
@media (max-width: 460px) { .rule-row { grid-template-columns: 1fr; } }
</style>
