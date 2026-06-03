<template>
  <MonacoEditor
    :model-value="code"
    :language="language"
    :readonly="true"
    :height="height"
    :highlight-lines="highlightLines"
    ref="editorRef"
  />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'

defineProps<{
  code: string
  language?: string
  height?: string
  highlightLines?: number[]
}>()

const editorRef = ref<InstanceType<typeof MonacoEditor> | null>(null)

function revealLine(lineNumber: number) {
  editorRef.value?.revealLine(lineNumber)
}

defineExpose({ revealLine })
</script>
