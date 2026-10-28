<template>
  <button @click="external = !external">{{ external ? '返回写作流程' : '查看设置流程' }}</button>
  <p data-testid="selected-writing-node">{{ store.selectedNodeId || 'none' }}</p>
  <p data-testid="opened-node-detail">{{ detail || 'none' }}</p>
  <div style="width: 900px; height: 600px"><DAGCanvas novel-id="book" :nodes="external ? externalNodes : undefined" :edges="external ? [] : undefined" @node-detail="detail = $event" /></div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import DAGCanvas from '../../../src/components/autopilot/DAGCanvas.vue'
import { useDAGStore } from '../../../src/stores/dagStore'
const external = ref(false), detail = ref('')
const store = useDAGStore()
store.dagDefinition = { id: 'writing', name: 'Writing', version: 1, description: '', nodes: [{ id: 'writer', type: 'exec_writer', label: '写作节点', position: { x: 0, y: 0 }, enabled: true, config: {} }], edges: [], metadata: { created_at: '', updated_at: '', created_by: 'fixture' } }
const externalNodes = [{ id: 'setup-audit', type: 'dagCustom', position: { x: 0, y: 0 }, data: { id: 'setup-audit', type: 'setup_step', label: '设置审核节点', enabled: true, isSelected: false, meta: { display_name: '只读设置审核' } } }]
</script>
