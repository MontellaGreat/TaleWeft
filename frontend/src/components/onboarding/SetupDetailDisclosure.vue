<template>
  <details :open="expanded" @toggle="onToggle">
    <summary>{{ title }}</summary>
    <div v-if="expanded" class="setup-detail-disclosure__content"><slot /></div>
  </details>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
const props = withDefaults(defineProps<{ title: string; initiallyOpen?: boolean }>(), { initiallyOpen: false })
const expanded = ref(props.initiallyOpen)
watch(() => props.initiallyOpen, value => { expanded.value = value })
function onToggle(event: Event) { expanded.value = (event.currentTarget as HTMLDetailsElement).open }
</script>

<style scoped>
summary { cursor: pointer; padding: 8px 0; color: var(--app-text-secondary); }
summary:focus-visible { outline: 2px solid var(--app-accent, #527765); outline-offset: 3px; border-radius: 3px; }
.setup-detail-disclosure__content { display: grid; gap: 10px; min-width: 0; }
</style>
