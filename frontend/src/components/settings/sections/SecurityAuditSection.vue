<template>
  <n-space vertical>
    <n-alert type="info">记录 API 修改请求和被拒绝的访问。记录包含时间、接口模板与结果，保留最多 3 MB，不保存请求正文、凭据或查询参数。</n-alert>
    <n-button :loading="busy" @click="refresh">刷新记录</n-button>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <n-empty v-else-if="!events.length" description="暂无审计记录" />
    <n-data-table v-else :columns="columns" :data="events" :pagination="{ pageSize: 15 }" />
  </n-space>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NButton, NDataTable, NEmpty, NSpace } from 'naive-ui'
import { apiClient } from '@/api/config'
interface Event { time: string; method: string; route: string; status: number; outcome: string }
const events = ref<Event[]>([]), busy = ref(false), error = ref('')
const columns = [
  { title: '时间', key: 'time' }, { title: '方法', key: 'method' },
  { title: '接口', key: 'route' }, { title: '状态码', key: 'status' },
  { title: '结果', key: 'outcome' },
]
async function refresh() {
  busy.value = true; error.value = ''
  try {
    const data = await apiClient.get<{ status: string; events: Event[] }>('/settings/security-audit')
    events.value = data.events
    if (data.status === 'unavailable') error.value = '审计记录写入不可用，请检查本机数据目录'
  } catch { error.value = '无法读取审计记录' }
  finally { busy.value = false }
}
onMounted(refresh)
</script>
