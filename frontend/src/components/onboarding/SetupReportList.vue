<template>
  <div class="setup-report-list">
    <div class="setup-report-list__items"><slot v-for="(item, index) in visibleItems" :key="offset + index" :item="item" :index="offset + index" /></div>
    <nav v-if="pages > 1" class="setup-report-list__pagination" :aria-label="`${label}分页`">
      <span aria-live="polite">{{ label }} · 第 {{ currentPage }} / {{ pages }} 页 · 共 {{ items.length }} 项</span>
      <div>
        <button type="button" :aria-label="`${label}首页`" :disabled="currentPage === 1" @click="page = 1">首页</button>
        <button type="button" :aria-label="`${label}上一页`" :disabled="currentPage === 1" @click="page = currentPage - 1">上一页</button>
        <button type="button" :aria-label="`${label}下一页`" :disabled="currentPage === pages" @click="page = currentPage + 1">下一页</button>
        <button type="button" :aria-label="`${label}末页`" :disabled="currentPage === pages" @click="page = pages">末页</button>
      </div>
    </nav>
  </div>
</template>

<script setup lang="ts" generic="T">
import { computed, ref } from 'vue'
const props = withDefaults(defineProps<{ items: T[]; label: string; pageSize?: number }>(), { pageSize: 8 })
const page = ref(1)
const pages = computed(() => Math.max(1, Math.ceil(props.items.length / props.pageSize)))
const currentPage = computed(() => Math.min(page.value, pages.value))
const offset = computed(() => (currentPage.value - 1) * props.pageSize)
const visibleItems = computed(() => props.items.slice(offset.value, offset.value + props.pageSize))
</script>

<style scoped>
.setup-report-list,.setup-report-list__items { display: grid; gap: 10px; min-width: 0; }
.setup-report-list__pagination { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; font-variant-numeric: tabular-nums; }
.setup-report-list__pagination > div { display: flex; flex-wrap: wrap; gap: 6px; }
button { padding: 5px 9px; border: 1px solid var(--app-border); border-radius: 5px; background: var(--app-surface); color: var(--app-text-primary); cursor: pointer; touch-action: manipulation; font: inherit; }
button:hover:enabled { border-color: var(--app-accent, #527765); }
button:focus-visible { outline: 2px solid var(--app-accent, #527765); outline-offset: 2px; }
button:disabled { opacity: .45; cursor: default; }
</style>
