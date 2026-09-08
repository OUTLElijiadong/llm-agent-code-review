<template>
  <div class="monaco-editor-wrapper" :style="{ height: editorHeight }">
    <div ref="editorContainer" class="monaco-editor-surface" />
    <span v-if="locatedLine !== null" class="code-location-hint" role="status">已定位到第 {{ locatedLine }} 行</span>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { usePreferredReducedMotion } from '@vueuse/core'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'
import { ensurePrismMonacoThemes, PRISM_LIGHT_THEME, PRISM_DARK_THEME } from './prismMonacoTheme'

type PrismMonacoEnvironment = {
  getWorker: (workerId: string, label: string) => Worker
}

/**
 * 为 Monaco 按语言创建 Vite Worker，避免编辑器降级到主线程。
 * @param _workerId - Monaco 分配的 Worker 标识。
 * @param label - Monaco 请求的语言服务类型。
 * @returns 对应语言服务的 Worker 实例。
 */
function getMonacoWorker(_workerId: string, label: string): Worker {
  if (label === 'json') return new jsonWorker()
  if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker()
  if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker()
  if (label === 'typescript' || label === 'javascript') return new tsWorker()
  return new editorWorker()
}

;(self as typeof self & { MonacoEnvironment: PrismMonacoEnvironment }).MonacoEnvironment = {
  getWorker: getMonacoWorker,
}

const props = withDefaults(defineProps<{
  modelValue: string
  language?: string
  readonly?: boolean
  height?: string
  highlightLines?: number[]
  theme?: 'light' | 'dark'
}>(), {
  language: 'text',
  readonly: false,
  height: '400px',
  highlightLines: () => [],
  theme: 'light',
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const editorContainer = ref<HTMLElement | null>(null)
const editorHeight = ref(props.height)
let editor: monaco.editor.IStandaloneCodeEditor | null = null
let decorations: string[] = []
let locationDecorations: string[] = []
const locatedLine = ref<number | null>(null)
const reducedMotion = usePreferredReducedMotion()

function resolveTheme() {
  return props.theme === 'dark' ? PRISM_DARK_THEME : PRISM_LIGHT_THEME
}

function createEditor() {
  if (!editorContainer.value) return

  ensurePrismMonacoThemes(monaco)

  editor = monaco.editor.create(editorContainer.value, {
    value: props.modelValue,
    language: props.language,
    theme: resolveTheme(),
    readOnly: props.readonly,
    automaticLayout: true,
    minimap: { enabled: false },
    lineNumbers: 'on',
    scrollBeyondLastLine: false,
    wordWrap: 'on',
    fontFamily: '"JetBrains Mono", "SF Mono", Consolas, Menlo, monospace',
    fontSize: 13,
    lineHeight: 22,
    tabSize: 2,
    renderLineHighlight: 'all',
    roundedSelection: true,
    padding: { top: 12, bottom: 12 },
  })

  if (!props.readonly) {
    editor.onDidChangeModelContent(() => {
      const value = editor?.getValue() ?? ''
      emit('update:modelValue', value)
    })
  }

  applyHighlights()
}

function applyHighlights() {
  if (!editor) return

  decorations = editor.deltaDecorations(decorations, props.highlightLines.map((line) => ({
    range: new monaco.Range(line, 1, line, 1),
    options: {
      isWholeLine: true,
      className: 'prism-line-issue',
      linesDecorationsClassName: 'prism-line-marker',
    },
  })))
}

function clearLocation(): void {
  locatedLine.value = null
  if (editor) locationDecorations = editor.deltaDecorations(locationDecorations, [])
}

function revealLine(lineNumber: number) {
  const model = editor?.getModel()
  if (!editor || !model || !Number.isInteger(lineNumber) || lineNumber < 1 || lineNumber > model.getLineCount()) return
  editor.revealLineInCenter(lineNumber, reducedMotion.value === 'reduce' ? monaco.editor.ScrollType.Immediate : monaco.editor.ScrollType.Smooth)
  editor.setPosition({ lineNumber, column: 1 })
  editor.focus()
  locatedLine.value = lineNumber
  locationDecorations = editor.deltaDecorations(locationDecorations, [{
    range: new monaco.Range(lineNumber, 1, lineNumber, 1),
    options: { isWholeLine: true, className: 'prism-line-located' },
  }])
}

watch(() => props.modelValue, (val) => {
  clearLocation()
  if (editor && val !== editor.getValue()) {
    editor.setValue(val)
  }
})

watch(() => props.language, (lang) => {
  if (editor) {
    const model = editor.getModel()
    if (model) monaco.editor.setModelLanguage(model, lang)
  }
})

watch(() => props.theme, () => {
  if (editor) monaco.editor.setTheme(resolveTheme())
})

watch(() => props.highlightLines, () => {
  applyHighlights()
}, { deep: true })

onMounted(() => {
  createEditor()
})

onBeforeUnmount(() => {
  if (editor) editor.dispose()
  editor = null
})

/** 透传 monaco editor.updateOptions(代码字号缩放等)。 */
function updateOptions(options: Record<string, unknown>): void {
  editor?.updateOptions(options as never)
}

defineExpose({ revealLine, updateOptions })
</script>

<style scoped lang="scss">
.monaco-editor-wrapper {
  position: relative;
  width: 100%;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}
.monaco-editor-surface { width: 100%; height: 100%; }
.code-location-hint {
  position: absolute;
  right: 24px;
  bottom: 8px;
  z-index: 2;
  padding: 2px 7px;
  border-radius: 4px;
  background: var(--brand-50, #efeeff);
  color: var(--brand-700, #3f3baf);
  font-size: 11px;
  pointer-events: none;
}
</style>

<style>
.prism-line-issue {
  background: rgba(220, 73, 97, 0.10) !important;
}
.prism-line-marker {
  background: linear-gradient(180deg, #DC4961, #E25C73);
  width: 3px !important;
  margin-left: 3px;
}
.prism-line-located { animation: prism-code-locate 600ms ease-out; }
@keyframes prism-code-locate {
  from { background: rgba(91, 88, 232, 0.24); }
  to { background: transparent; }
}
@media (prefers-reduced-motion: reduce) {
  .prism-line-located { animation: none; }
}
</style>
