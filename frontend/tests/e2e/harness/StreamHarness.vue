<template>
  <button @click="run" :disabled="running">开始生成</button>
  <pre data-testid="chunks">{{ chunks }}</pre>
  <p data-testid="done">{{ done }}</p>
  <p data-testid="terminal">{{ terminal }}</p>
  <p data-testid="error" role="alert">{{ error }}</p>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { consumeGenerateChapterStream } from '../../../src/api/workflow'
const chunks = ref(''), done = ref(''), terminal = ref(''), error = ref(''), running = ref(false)
async function run() {
  running.value = true
  await consumeGenerateChapterStream('book', { chapter_number: 1, outline: 'browser smoke' }, {
    onChunk: text => { chunks.value += text },
    onDone: result => { done.value = result.content },
    onTerminal: status => { terminal.value = status },
    onError: message => { error.value = message },
  })
  running.value = false
}
</script>
