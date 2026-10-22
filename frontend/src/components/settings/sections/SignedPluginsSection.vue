<template>
  <n-space vertical :size="18">
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <n-alert type="info">插件包提供提示词和文风规则。添加发布者前，请通过可信渠道核对公钥；安装前会验证签名并展示包内权限。</n-alert>
    <n-text strong>可信发布者</n-text>
    <n-text v-if="!keys.length" depth="3">还没有可信发布者。填写公钥与允许的内容类型后，可以验证该发布者的插件。</n-text>
    <div v-for="key in keys" :key="key.id" class="entry">
      <n-space justify="space-between" align="center"><n-text>{{ key.name }} ({{ key.id }})</n-text><n-button :disabled="busy" @click="revoke(key)">移除信任</n-button></n-space>
      <n-text depth="3">允许：{{ permissionLabels(key.permissions) }}</n-text>
      <n-text depth="3" class="digest">公钥指纹 {{ key.fingerprint }}</n-text>
    </div>
    <n-form label-placement="top">
      <div class="key-fields">
        <n-form-item label="发布者标识"><n-input v-model:value="signer.id" :disabled="busy" placeholder="例如 writer-studio" aria-label="发布者标识" /></n-form-item>
        <n-form-item label="发布者名称"><n-input v-model:value="signer.name" :disabled="busy" placeholder="供作者识别的名称" aria-label="发布者名称" /></n-form-item>
      </div>
      <n-form-item label="Ed25519 公钥（Base64）"><n-input v-model:value="signer.public_key" :disabled="busy" aria-label="发布者公钥" /></n-form-item>
      <n-form-item label="允许提供的内容"><n-select v-model:value="signer.permissions" multiple :options="permissionOptions" :disabled="busy" aria-label="发布者允许权限" /></n-form-item>
    </n-form>
    <n-button :disabled="busy || !signer.id || !signer.name || !signer.public_key || !signer.permissions.length" @click="addKey">添加可信发布者</n-button>
    <n-divider>安装插件</n-divider>
    <label class="file-input">选择签名插件 JSON <input type="file" accept=".json,application/json" :disabled="busy" @change="readFile" /></label>
    <n-input v-model:value="packageText" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" placeholder="或粘贴完整签名插件包" aria-label="签名插件包" :disabled="busy" />
    <n-space><n-button :disabled="busy || !packageText.trim()" @click="verify">验证签名与权限</n-button><n-button type="primary" :disabled="busy || !preview" @click="install">安装已验证插件</n-button></n-space>
    <template v-if="preview">
      <n-text>{{ preview.name }} {{ preview.version }}，发布者 {{ preview.signer_id }}</n-text>
      <n-text>权限：{{ permissionLabels(preview.permissions) }}。提示词 {{ preview.prompt_count }} 个，文风规则 {{ preview.rule_count }} 条。</n-text>
      <n-text depth="3" class="digest">验证通过，内容哈希 {{ preview.digest }}</n-text>
    </template>
    <n-divider>已安装插件</n-divider>
    <n-text v-if="!packages.length" depth="3">尚未安装插件。</n-text>
    <div v-for="plugin in packages" :key="plugin.id" class="entry">
      <n-space justify="space-between" align="center"><n-text>{{ plugin.name }} {{ plugin.version }}</n-text><n-button :disabled="busy" @click="uninstall(plugin)">卸载插件</n-button></n-space>
      <n-text depth="3">发布者 {{ plugin.signer_id }}；提示词 {{ plugin.prompt_count }} 个，文风规则 {{ plugin.rule_count }} 条</n-text>
    </div>
    <n-button :disabled="busy" @click="load">刷新插件列表</n-button>
  </n-space>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { NAlert, NButton, NDivider, NForm, NFormItem, NInput, NSelect, NSpace, NText, useDialog, useMessage } from 'naive-ui'
import { extensionPolicyApi, extensionError, type InstalledPlugin, type PluginPackage, type PluginPreview, type TrustedPluginKey, type PluginPermission } from '@/api/extensionPolicy'
const keys = ref<TrustedPluginKey[]>([]), packages = ref<InstalledPlugin[]>([]), packageText = ref(''), preview = ref<PluginPreview | null>(null)
const signer = reactive({ id: '', name: '', public_key: '', permissions: ['prompts', 'quality_rules'] as PluginPermission[] })
const busy = ref(false), error = ref(''), verifiedText = ref('')
const message = useMessage(), dialog = useDialog()
const permissionOptions = [{ label: '提示词', value: 'prompts' }, { label: '文风规则', value: 'quality_rules' }]
const permissionLabels = (values: PluginPermission[]) => values.map(value => value === 'prompts' ? '提示词' : '文风规则').join('、')
watch(packageText, () => { preview.value = null; verifiedText.value = '' })
async function run(action: () => Promise<void>) { busy.value = true; error.value = ''; try { await action() } catch (e) { error.value = extensionError(e) } finally { busy.value = false } }
async function refresh() { const state = await extensionPolicyApi.plugins(); keys.value = state.keys; packages.value = state.packages }
async function load() { await run(refresh) }
async function addKey() { await run(async () => { await extensionPolicyApi.trust({ ...signer, public_key: signer.public_key.trim(), permissions: [...signer.permissions] }); await refresh(); signer.public_key = ''; message.success('可信发布者已添加') }) }
async function readFile(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; await run(async () => { if (file.size > 2_000_000) throw new Error('插件包超过2MB限制'); packageText.value = await file.text() }) }
async function verify() { const original = packageText.value; await run(async () => { const result = await extensionPolicyApi.verify(JSON.parse(original) as PluginPackage); if (packageText.value === original) { preview.value = result; verifiedText.value = original } }) }
async function install() { const checked = preview.value; if (!checked || packageText.value !== verifiedText.value) return; const data = JSON.parse(verifiedText.value) as PluginPackage; await run(async () => { await extensionPolicyApi.install(data, checked.digest); await refresh(); preview.value = null; message.success('已验证插件已安装') }) }
function revoke(key: TrustedPluginKey) { dialog.warning({ title: '移除发布者信任', content: `移除 ${key.name} 的信任会同时卸载其插件提示词与规则。已有审计保留。`, positiveText: '确认移除信任', negativeText: '取消', onPositiveClick: async () => { await run(async () => { await extensionPolicyApi.revoke(key.id); preview.value = null; await refresh(); message.success('发布者信任已移除') }) } }) }
function uninstall(plugin: InstalledPlugin) { dialog.warning({ title: '卸载插件', content: `卸载 ${plugin.name} 的提示词与规则？已有审计保留。`, positiveText: '确认卸载插件', negativeText: '取消', onPositiveClick: async () => { await run(async () => { await extensionPolicyApi.uninstall(plugin.id); await refresh(); message.success('插件已卸载') }) } }) }
onMounted(load)
</script>

<style scoped>
.entry { display: flex; flex-direction: column; gap: 6px; padding-block: 8px; }
.digest { overflow-wrap: anywhere; }
.key-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.file-input { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
@media (max-width: 600px) { .key-fields { grid-template-columns: 1fr; gap: 0; } }
</style>
