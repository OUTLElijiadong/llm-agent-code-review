<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { CopyDocument, Download, MagicStick } from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import { renderMarkdown } from '@/utils/markdown'
import {
  listAiPromptTools,
  generatePromptForIssue,
  generatePromptForProject,
  generatePromptForTask,
} from '@/api/aiPrompt'
import type {
  AiPromptBundleOut,
  AiPromptItemOut,
  AiPromptToolOut,
} from '@/types/aiPrompt'

interface Props {
  modelValue: boolean
  source: 'issue' | 'task' | 'project'
  /** issue/task/project 模式: 传对应 id */
  refId: number | null
  initialTool?: string
}

const props = withDefaults(defineProps<Props>(), {
  initialTool: 'generic',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const tools = ref<AiPromptToolOut[]>([])
const targetTool = ref<string>(props.initialTool)
const useLlm = ref<boolean>(true)
const loading = ref(false)
const bundle = ref<AiPromptBundleOut | null>(null)
const activeIdx = ref(0)

// 聚合「一键修复全部」条目排在最前,其后是逐条修复提示词
const displayItems = computed<AiPromptItemOut[]>(() => {
  const b = bundle.value
  if (!b) return []
  return [...(b.aggregates ?? []), ...b.prompts]
})
const activeItem = computed<AiPromptItemOut | null>(
  () => displayItems.value[activeIdx.value] ?? null,
)

async function loadTools(): Promise<void> {
  if (tools.value.length) return
  try {
    tools.value = await listAiPromptTools()
  } catch {
    tools.value = [
      { value: 'generic', label: '通用 AI' },
      { value: 'cursor', label: 'Cursor' },
      { value: 'copilot', label: 'GitHub Copilot Chat' },
      { value: 'chatgpt', label: 'ChatGPT' },
      { value: 'claude_code', label: 'Claude Code' },
    ]
  }
}

/**
 * AI 润色会向已配置的模型服务发送代码片段，生成前必须由用户显式确认。
 *
 * @returns 用户是否允许继续生成。
 */
async function confirmExternalPolish(): Promise<boolean> {
  if (!useLlm.value) return true
  try {
    await ElMessageBox.confirm(
      '继续后将把相关代码片段发送给已配置的模型服务用于润色。是否确认生成？',
      '确认发送代码',
      {
        confirmButtonText: '确认生成',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    return true
  } catch {
    return false
  }
}

async function generate(): Promise<void> {
  if (props.refId === null) {
    ElMessage.warning('缺少必要的 ID 参数')
    return
  }
  if (!(await confirmExternalPolish())) return
  loading.value = true
  bundle.value = null
  activeIdx.value = 0
  try {
    if (props.source === 'issue') {
      bundle.value = await generatePromptForIssue({
        issue_id: props.refId,
        target_tool: targetTool.value,
        use_llm: useLlm.value,
      })
    } else if (props.source === 'task') {
      bundle.value = await generatePromptForTask({
        task_id: props.refId,
        target_tool: targetTool.value,
        use_llm: useLlm.value,
      })
    } else {
      bundle.value = await generatePromptForProject({
        project_id: props.refId,
        target_tool: targetTool.value,
        top_n: 30,
        use_llm: useLlm.value,
      })
    }
    if (!bundle.value?.prompts?.length) {
      ElMessage.warning('未生成任何提示词')
    }
  } finally {
    loading.value = false
  }
}

async function copyPrompt(p: AiPromptItemOut): Promise<void> {
  try {
    await navigator.clipboard.writeText(p.prompt_text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败,请手动选择文本复制')
  }
}

function downloadAll(): void {
  if (!displayItems.value.length) return
  const md = displayItems.value
    .map((p, i) =>
      [
        `## ${i + 1}. ${p.title}`,
        '',
        `**目标工具**: ${p.target_label}`,
        '',
        '```',
        p.prompt_text,
        '```',
      ].join('\n'),
    )
    .join('\n\n---\n\n')
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `prism_ai_prompts_${props.source}_${props.refId}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * 参数变化后清空旧结果，避免展示与当前选项不一致的提示词。
 *
 * @returns 无返回值。
 */
function resetGeneratedBundle(): void {
  bundle.value = null
  activeIdx.value = 0
}

watch(visible, (v) => {
  if (v) loadTools()
})

watch([() => props.source, () => props.refId, targetTool, useLlm], resetGeneratedBundle)
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="source === 'issue' ? '生成 AI 修复提示词' : (source === 'project' ? '生成项目 AI 修复手册' : '生成 AI 修复包')"
    width="720px"
    :close-on-click-modal="false"
    top="6vh"
  >
    <div class="prompt-toolbar">
      <div class="tb-left">
        <span class="tb-label">目标工具</span>
        <el-select v-model="targetTool" size="small" style="width: 200px">
          <el-option
            v-for="t in tools"
            :key="t.value"
            :label="t.label"
            :value="t.value"
          />
        </el-select>
      </div>
      <div class="tb-right">
        <el-checkbox v-model="useLlm" size="small">让 AI 润色一遍</el-checkbox>
        <el-button
          size="small"
          type="primary"
          :icon="MagicStick"
          :loading="loading"
          @click="generate"
        >
          {{ bundle ? '重新生成' : '生成' }}
        </el-button>
        <el-button
          v-if="displayItems.length"
          size="small"
          :icon="Download"
          @click="downloadAll"
        >
          下载 Markdown
        </el-button>
      </div>
    </div>

    <div class="generation-notice">
      {{ useLlm
        ? '点击生成后，将在确认后把相关代码片段发送给已配置的模型服务进行润色。'
        : '当前关闭 AI 润色：点击生成后仅使用本地模板，不调用模型服务。' }}
    </div>

    <div class="prompt-summary" v-if="bundle?.summary" v-html="renderMarkdown(bundle.summary)" />

    <div v-if="loading" class="prompt-loading">
      正在为你生成可粘贴的 AI 修复提示词…
    </div>

    <div v-else-if="displayItems.length" class="prompt-body">
      <aside v-if="displayItems.length > 1" class="prompt-list">
        <button
          v-for="(p, i) in displayItems"
          :key="(p.kind || 'issue') + '-' + p.issue_id"
          class="list-item"
          :class="{ active: activeIdx === i, 'is-aggregate': p.kind === 'aggregate' }"
          @click="activeIdx = i"
        >
          <span v-if="p.kind === 'aggregate'" class="agg-badge">🛠</span>
          <span v-else class="sev" :class="`sev-${p.severity}`">{{ p.severity }}</span>
          <div class="info">
            <div class="title">{{ p.title }}</div>
            <div class="sub">{{ p.file_path }} · {{ p.lines }}</div>
          </div>
        </button>
      </aside>

      <main v-if="activeItem" class="prompt-view">
        <header class="view-head">
          <div class="vh-left">
            <span class="vh-tool">{{ activeItem.target_label }}</span>
            <span v-if="activeItem.kind === 'aggregate'" class="vh-agg">一键修复全部</span>
            <span class="vh-where font-mono">
              {{ activeItem.file_path }} · {{ activeItem.lines }}
            </span>
          </div>
          <el-button
            size="small"
            type="primary"
            :icon="CopyDocument"
            @click="copyPrompt(activeItem)"
          >
            一键复制
          </el-button>
        </header>
        <pre class="prompt-text">{{ activeItem.prompt_text }}</pre>
      </main>
    </div>

    <div v-else class="prompt-empty">
      还未生成提示词,点击「生成」开始
    </div>
  </el-dialog>
</template>

<style scoped lang="scss">
.prompt-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--gray-100);
  margin-bottom: 12px;
}

.generation-notice {
  margin: 0 0 12px;
  color: var(--gray-600);
  font-size: 12px;
  line-height: 1.6;
}

.tb-left {
  display: flex;
  align-items: center;
  gap: 8px;

  .tb-label {
    font-size: 12px;
    color: var(--gray-600);
  }
}

.tb-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.prompt-summary {
  background: var(--brand-50);
  border-left: 3px solid var(--brand-500);
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 12.5px;
  color: var(--brand-700);
  margin-bottom: 12px;

  :deep(p) { margin: 0 0 6px; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(ul), :deep(ol) { padding-left: 18px; margin: 4px 0; }
  :deep(strong) { font-weight: 600; }
  :deep(code) { background: var(--gray-100); padding: 0 4px; border-radius: 3px; font-size: 11.5px; }
}

.prompt-loading,
.prompt-empty {
  text-align: center;
  padding: 40px;
  color: var(--gray-500);
  font-size: 13px;
}

.prompt-body {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 14px;
  max-height: 60vh;
  min-height: 320px;
}

.prompt-body:has(.prompt-list:empty),
.prompt-body:not(:has(.prompt-list)) {
  grid-template-columns: 1fr;
}

.prompt-list {
  background: var(--gray-50);
  border-radius: 8px;
  padding: 6px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.list-item {
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 8px 10px;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px;
  align-items: flex-start;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover { background: #fff; }
  &.active {
    background: #fff;
    border-color: var(--brand-300);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
  }

  .sev {
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 3px;
    color: #fff;
    font-weight: 600;
    white-space: nowrap;
  }

  .sev-严重 { background: #DC4961; }
  .sev-高   { background: #E27C4A; }
  .sev-中   { background: #D9A857; }
  .sev-低   { background: #4FB87A; }

  .info { min-width: 0; }

  .title {
    font-size: 12px;
    font-weight: 500;
    color: var(--gray-900);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sub {
    font-size: 10.5px;
    color: var(--gray-500);
    font-family: var(--font-mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

/* 一键修复全部 —— 列表中高亮置顶 */
.list-item.is-aggregate {
  background: var(--brand-50);
  border-color: var(--brand-200);

  &:hover { background: var(--brand-100, #eef0ff); }
  &.active { border-color: var(--brand-400); }

  .title { color: var(--brand-700); font-weight: 600; }
}

.agg-badge {
  width: 22px;
  font-size: 14px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.vh-agg {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--brand-500);
  color: #fff;
  font-weight: 600;
}

.prompt-view {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  overflow: hidden;
  min-height: 0;
}

.view-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-50);
}

.vh-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.vh-tool {
  font-size: 11.5px;
  padding: 2px 8px;
  background: var(--brand-50);
  color: var(--brand-700);
  border-radius: 4px;
  font-weight: 500;
}

.vh-where {
  font-size: 11px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prompt-text {
  margin: 0;
  padding: 14px 16px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-y: auto;
  flex: 1;
  background: #fff;
  color: var(--gray-800);
}
</style>
