<template>
  <el-drawer v-model="visible" :title="title" size="500px" direction="rtl" @close="onClose">
    <template v-if="issue">
      <div class="drawer-section">
        <div class="drawer-label">基本信息</div>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="严重程度">
            <SeverityTag :severity="issue.severity" />
          </el-descriptions-item>
          <el-descriptions-item label="问题类型">
            <el-tag size="small" type="info">{{ issueTypeLabel(issue.issue_type) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="文件">{{ issue.file_name ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="行号">{{ issue.line_number ? `${issue.line_number}${issue.end_line ? ` ~ ${issue.end_line}` : ''}` : '-' }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(issue.status)" size="small">{{ statusLabel(issue.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="发现时间">{{ formatDateTime(issue.create_time) }}</el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="drawer-section">
        <div class="drawer-label">问题描述</div>
        <div class="drawer-content">{{ issue.description }}</div>
      </div>

      <div v-if="issue.suggestion" class="drawer-section">
        <div class="drawer-label">修复建议</div>
        <div class="drawer-content">{{ issue.suggestion }}</div>
      </div>

      <div v-if="issue.fixed_code" class="drawer-section">
        <div class="drawer-label">修复后代码</div>
        <div class="drawer-code">
          <pre><code>{{ issue.fixed_code }}</code></pre>
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-label">交给其他 AI 修复</div>
        <p class="drawer-hint">
          生成可粘贴给 Cursor / Copilot / ChatGPT / Claude Code 的修复提示词,
          自动包含文件路径、行号、问题描述和棱镜的修复建议。
        </p>
        <el-button
          type="primary"
          :icon="MagicStick"
          @click="openAiPrompt"
        >
          生成 AI 修复提示词
        </el-button>
      </div>
    </template>

    <AiPromptModal
      v-model="promptVisible"
      source="issue"
      :ref-id="issue?.id ?? null"
    />
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { formatDateTime } from '@/utils/format'
import { MagicStick } from '@element-plus/icons-vue'
import SeverityTag from './SeverityTag.vue'
import AiPromptModal from './AiPromptModal.vue'
import type { IssueOut } from '@/types/review'

const props = defineProps<{
  modelValue: boolean
  issue: IssueOut | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (val) => emit('update:modelValue', val),
})

const title = computed(() => props.issue?.title ?? '问题详情')

const issueTypeLabels: Record<string, string> = {
  style: '代码规范',
  security: '安全漏洞',
  performance: '性能问题',
  logic: '逻辑错误',
  maintainability: '可维护性',
}

const statusLabels: Record<string, string> = {
  unfixed: '未修复',
  fixed: '已修复',
  ignored: '已忽略',
  pending_review: '待审核',
}

const statusTypeMap: Record<string, string> = {
  unfixed: 'danger',
  fixed: 'success',
  ignored: 'info',
  pending_review: 'warning',
}

function issueTypeLabel(type: string) {
  return issueTypeLabels[type] ?? type
}

function statusLabel(status: string) {
  return statusLabels[status] ?? status
}

function statusType(status: string) {
  return statusTypeMap[status] ?? 'info'
}

const promptVisible = ref(false)

function openAiPrompt() {
  if (!props.issue) return
  promptVisible.value = true
}

function onClose() {
  promptVisible.value = false
  emit('update:modelValue', false)
}
</script>

<style scoped lang="scss">
.drawer-hint {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--gray-600);
  line-height: 1.6;
}

.drawer-section {
  margin-bottom: 20px;

  .drawer-label {
    font-size: 14px;
    font-weight: 600;
    color: var(--el-text-color-primary);
    margin-bottom: 8px;
    padding-left: 4px;
    border-left: 3px solid var(--el-color-primary);
  }

  .drawer-content {
    font-size: 13px;
    line-height: 1.8;
    color: var(--el-text-color-regular);
    padding: 8px 12px;
    background: var(--el-fill-color-light);
    border-radius: 4px;
  }

  .drawer-code {
    background: #1e1e1e;
    border-radius: 4px;
    padding: 12px;
    overflow-x: auto;

    pre {
      margin: 0;
      code {
        color: #d4d4d4;
        font-size: 12px;
        line-height: 1.6;
        font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
      }
    }
  }
}
</style>
