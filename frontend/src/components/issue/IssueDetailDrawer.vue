<template>
  <el-drawer v-model="visible" :title="title" size="560px" direction="rtl" @close="onClose">
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
          <!-- v3: 来源 + 静态规则命中次数 -->
          <el-descriptions-item v-if="issue.source" label="来源">
            {{ sourceLabel(issue.source) }}
          </el-descriptions-item>
          <el-descriptions-item v-if="issue.static_rule_hits != null && issue.static_rule_hits > 0" label="静态命中">
            <el-tag size="small" type="warning" effect="plain">{{ issue.static_rule_hits }} 次</el-tag>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="drawer-section">
        <div class="drawer-label">问题描述</div>
        <div class="drawer-content">{{ issue.description }}</div>
      </div>

      <!-- v3: CVSS 评分 + 向量字符串 -->
      <div v-if="issue.cvss_score != null || issue.cvss_vector" class="drawer-section">
        <div class="drawer-label">CVSS 评分</div>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item v-if="issue.cvss_score != null" label="评分">
            <el-tag size="small" :type="cvssTagType(issue.cvss_score)" effect="dark">
              {{ issue.cvss_score.toFixed(1) }}
            </el-tag>
            <span class="cvss-level-text">{{ cvssLevelText(issue.cvss_score) }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="issue.cvss_vector" label="向量">
            <code class="cvss-vector">{{ issue.cvss_vector }}</code>
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <!-- v3: 合规映射详情(4 个标准的命中条款列表) -->
      <div v-if="issue.compliance_mapping && hasComplianceHits(issue.compliance_mapping)" class="drawer-section">
        <div class="drawer-label">合规映射</div>
        <div class="compliance-list">
          <div
            v-for="std in complianceStandardMeta"
            :key="std.code"
            class="compliance-item"
          >
            <div class="compliance-header">
              <el-tag size="small" type="warning" effect="plain">{{ std.label }}</el-tag>
              <span class="compliance-count">
                {{ getComplianceHits(issue.compliance_mapping!, std.code).length }} 条
              </span>
            </div>
            <div
              v-if="getComplianceHits(issue.compliance_mapping!, std.code).length > 0"
              class="compliance-clauses"
            >
              <el-tag
                v-for="(clause, idx) in getComplianceHits(issue.compliance_mapping!, std.code)"
                :key="idx"
                size="small"
                type="info"
                effect="plain"
                class="compliance-clause-tag"
              >
                {{ clause }}
              </el-tag>
            </div>
            <span v-else class="text-muted">无命中</span>
          </div>
        </div>
      </div>

      <!-- v3: 攻击场景说明 -->
      <div v-if="issue.exploit_scenario" class="drawer-section">
        <div class="drawer-label">攻击场景</div>
        <div class="drawer-content">{{ issue.exploit_scenario }}</div>
      </div>

      <!-- v3: 漏洞证据代码片段 -->
      <div v-if="issue.evidence" class="drawer-section">
        <div class="drawer-label">漏洞证据</div>
        <div class="drawer-code">
          <pre><code>{{ issue.evidence }}</code></pre>
        </div>
      </div>

      <div v-if="issue.suggestion" class="drawer-section">
        <div class="drawer-label">修复建议</div>
        <div class="drawer-content">{{ issue.suggestion }}</div>
      </div>

      <!-- v3: 详细修复方案(remediation,使用 <pre> 标签保留格式) -->
      <div v-if="issue.remediation" class="drawer-section">
        <div class="drawer-label">详细修复方案</div>
        <div class="drawer-code">
          <pre><code>{{ issue.remediation }}</code></pre>
        </div>
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
/**
 * 问题详情抽屉组件
 * v3 增强:
 *  - 新增 CVSS 评分 + 向量字符串展示区
 *  - 新增 合规映射详情(4 个标准 ISO27001/GDPR/PCI-DSS/HIPAA 的命中条款列表)
 *  - 新增 详细修复方案(remediation,使用 <pre> 保留格式)
 *  - 新增 静态规则命中次数(static_rule_hits)
 *  - 新增 攻击场景说明(exploit_scenario)
 *  - 新增 漏洞证据代码片段(evidence,使用 <pre><code> 高亮显示)
 *  - 字段缺失时不显示对应区块,避免空白
 */
import { computed, ref } from 'vue'
import { formatDateTime } from '@/utils/format'
import { MagicStick } from '@element-plus/icons-vue'
import SeverityTag from './SeverityTag.vue'
import AiPromptModal from './AiPromptModal.vue'
import type { IssueOut, ComplianceMapping } from '@/types/review'

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

/** 来源字段的中文标签映射 */
const sourceLabels: Record<string, string> = {
  LLM: 'LLM',
  llm: 'LLM',
  static: '静态',
  hybrid: '混合',
  mixed: '混合',
}

/** 合规标准元信息(key -> 标签) */
const complianceStandardMeta: { code: 'iso27001' | 'gdpr' | 'pci_dss' | 'hipaa'; label: string }[] = [
  { code: 'iso27001', label: 'ISO 27001' },
  { code: 'gdpr', label: 'GDPR' },
  { code: 'pci_dss', label: 'PCI-DSS' },
  { code: 'hipaa', label: 'HIPAA' },
]

/**
 * 获取问题类型的中文显示文案
 * @param type - 问题类型枚举值
 * @returns 中文问题类型文案
 */
function issueTypeLabel(type: string): string {
  return issueTypeLabels[type] ?? type
}

/**
 * 获取问题状态的中文显示文案
 * @param status - 问题状态枚举值
 * @returns 中文状态文案
 */
function statusLabel(status: string): string {
  return statusLabels[status] ?? status
}

/**
 * 获取 Element Plus 标签状态类型
 * @param status - 问题状态枚举值
 * @returns Element Plus Tag 的 type 值
 */
function statusType(status: string): string {
  return statusTypeMap[status] ?? 'info'
}

/**
 * 获取来源字段的中文标签
 * v3 新增
 * @param source - 来源字段值
 * @returns 中文标签
 */
function sourceLabel(source: string): string {
  return sourceLabels[source] ?? source
}

/**
 * 根据 CVSS 评分返回 Element Plus Tag 的 type
 * 9-10 红(danger) / 7-8.9 橙(warning) / 4-6.9 黄(primary) / 0-3.9 蓝(info)
 * v3 新增
 * @param score - CVSS 评分
 * @returns Element Plus Tag type
 */
function cvssTagType(score: number): 'danger' | 'warning' | 'primary' | 'info' {
  if (score >= 9) return 'danger'
  if (score >= 7) return 'warning'
  if (score >= 4) return 'primary'
  return 'info'
}

/**
 * 根据 CVSS 评分返回等级文案
 * v3 新增
 * @param score - CVSS 评分
 * @returns 等级中文文案
 */
function cvssLevelText(score: number): string {
  if (score >= 9) return '严重 Critical'
  if (score >= 7) return '高危 High'
  if (score >= 4) return '中危 Medium'
  return '低危 Low'
}

/**
 * 判断合规映射是否存在命中条款
 * v3 新增
 * @param mapping - 合规映射对象
 * @returns 是否有命中
 */
function hasComplianceHits(mapping: ComplianceMapping): boolean {
  return (
    (mapping.iso27001?.length ?? 0) > 0 ||
    (mapping.gdpr?.length ?? 0) > 0 ||
    (mapping.pci_dss?.length ?? 0) > 0 ||
    (mapping.hipaa?.length ?? 0) > 0
  )
}

/**
 * 获取指定标准的命中条款列表
 * v3 新增
 * @param mapping - 合规映射对象
 * @param code - 标准代码
 * @returns 命中条款数组
 */
function getComplianceHits(mapping: ComplianceMapping, code: 'iso27001' | 'gdpr' | 'pci_dss' | 'hipaa'): string[] {
  return mapping[code] ?? []
}

const promptVisible = ref(false)

/**
 * 打开 AI 修复提示词弹窗
 */
function openAiPrompt(): void {
  if (!props.issue) return
  promptVisible.value = true
}

/**
 * 抽屉关闭回调
 */
function onClose(): void {
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

/* v3: CVSS 评分等级文案 */
.cvss-level-text {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* v3: CVSS 向量字符串 */
.cvss-vector {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 12px;
  color: var(--el-text-color-regular);
  word-break: break-all;
}

/* v3: 合规映射列表 */
.compliance-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.compliance-item {
  background: var(--el-fill-color-light);
  border-radius: 4px;
  padding: 8px 12px;

  .compliance-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
  }

  .compliance-count {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  .compliance-clauses {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .compliance-clause-tag {
    margin: 0;
  }
}

.text-muted {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
</style>
