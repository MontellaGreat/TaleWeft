<template>
  <n-card size="small" title="全书模型用量与预算" style="margin-top: 12px">
    <n-space vertical>
      <n-space align="center">
        <n-select v-model:value="novelId" :options="novels" placeholder="选择小说" style="width: 240px" />
        <n-button size="small" :loading="loading" @click="refresh">刷新用量</n-button>
      </n-space>
      <template v-if="summary">
        <n-text>已记录 {{ summary.calls }} 次调用，含 {{ summary.historical_calls }} 次历史调用。</n-text>
        <n-text v-for="item in summary.currencies" :key="item.currency">
          {{ item.currency === 'UNKNOWN' ? '历史金额未知' : item.currency }} · 已知金额 {{ item.known_cost }}
          · 输入 {{ item.input_tokens }} / 输出 {{ item.output_tokens }} token
          · 总金额 {{ item.total_cost ?? '未知' }}
          · {{ item.unknown_usage_calls }} 次 usage 未回传 / {{ item.unknown_cost_calls }} 次金额未知
          <template v-if="item.currency !== 'UNKNOWN'"> · 实际金额与在途预留 {{ item.reserved_or_actual_cost }}</template>
        </n-text>
        <n-space align="center">
          <n-input v-model:value="currency" placeholder="币种，如 USD / CNY" style="width: 160px" />
          <n-input v-model:value="limit" placeholder="预算金额，留空表示不限" style="width: 230px" />
          <n-button size="small" :loading="saving" @click="saveBudget">保存小说预算</n-button>
        </n-space>
        <n-text depth="3" style="font-size: 12px">
          定价使用当前档案的每百万输入/输出 token 单价，并随调用冻结保存。预算约束账本中的实际金额与在途预留；未回传 usage 的调用会保留预留额度，缺定价或币种不一致会阻止新增调用。历史调用的金额保留为未知。
        </n-text>
        <n-collapse v-if="unknownEntries.length">
          <n-collapse-item title="按上游账单核对未知金额" name="reconcile">
            <n-space vertical>
              <n-select v-model:value="entryId" :options="unknownEntries.map(item => ({ value: item.id, label: `${item.model} · ${item.currency} · ${item.id.slice(0, 8)}` }))" placeholder="选择已结束的调用" />
              <n-input v-model:value="actualCost" placeholder="该调用在所示币种下的账单金额" />
              <n-input v-model:value="billingReason" placeholder="账单依据，例如账单日期与请求编号" :maxlength="500" />
              <n-button size="small" :loading="reconciling" :disabled="!entryId" @click="reconcile">保存账单核对记录</n-button>
              <n-text depth="3">核对仅补金额，未回传的 token 用量仍显示未知。记录保存后可用于预算判断。</n-text>
            </n-space>
          </n-collapse-item>
        </n-collapse>
      </template>
    </n-space>
  </n-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage } from 'naive-ui'
import { novelApi } from '@/api/novel'
import { llmControlApi, type BookModelUsage, type ModelUsageEntry } from '@/api/llmControl'
import { formatApiError } from '@/utils/apiError'

const route = useRoute()
const message = useMessage()
const novelId = ref<string | null>(null)
const novels = ref<Array<{ label: string; value: string }>>([])
const summary = ref<BookModelUsage | null>(null)
const currency = ref('USD')
const limit = ref('')
const loading = ref(false)
const saving = ref(false)
const entries = ref<ModelUsageEntry[]>([])
const unknownEntries = computed(() => entries.value.filter(item => item.cost === null && item.status !== 'running'))
const entryId = ref<string | null>(null)
const actualCost = ref('')
const billingReason = ref('')
const reconciling = ref(false)
let requestVersion = 0

async function refresh() {
  if (!novelId.value) return
  const id = novelId.value
  const version = ++requestVersion
  loading.value = true
  try {
    const [data, calls] = await Promise.all([llmControlApi.bookUsage(id), llmControlApi.bookUsageEntries(id)])
    if (version !== requestVersion) return
    summary.value = data
    entries.value = calls
    currency.value = data.budget?.currency || 'USD'
    limit.value = data.budget?.limit || ''
  } catch (error) {
    if (version === requestVersion) message.error(formatApiError(error))
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function reconcile() {
  if (!entryId.value) return
  reconciling.value = true
  try {
    await llmControlApi.reconcileUsage(entryId.value, actualCost.value.trim(), billingReason.value.trim())
    entryId.value = null
    actualCost.value = ''
    billingReason.value = ''
    await refresh()
    message.success('金额已按账单核对，token 未知状态保留')
  } catch (error) {
    message.error(formatApiError(error))
  } finally {
    reconciling.value = false
  }
}

async function saveBudget() {
  if (!novelId.value) return
  const id = novelId.value
  saving.value = true
  try {
    const data = await llmControlApi.saveBookBudget(id, currency.value, limit.value.trim() || null)
    if (novelId.value === id) summary.value = data
    message.success('小说模型预算已保存')
  } catch (error) {
    message.error(formatApiError(error))
  } finally {
    saving.value = false
  }
}

watch(novelId, () => { summary.value = null; entries.value = []; entryId.value = null; void refresh() })
onMounted(async () => {
  try {
    const items = await novelApi.listNovels()
    novels.value = items.map(novel => ({ label: novel.title, value: novel.id }))
    const selected = items.find(novel => novel.id === route.params.slug)
    novelId.value = selected?.id || items[0]?.id || null
  } catch (error) {
    message.error(formatApiError(error))
  }
})
</script>
