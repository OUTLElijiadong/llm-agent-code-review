<template>
  <el-drawer v-model="visible" :title="title" size="min(560px, 100vw)" direction="rtl" @close="onClose">
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

      <div v-if="hasAggregation" class="drawer-section">
        <div class="drawer-label">可信聚合</div>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="聚合版本">{{ issue.aggregation_version || '-' }}</el-descriptions-item>
          <el-descriptions-item label="真实来源">{{ issue.confirmation_count ?? issue.source_details?.length ?? 1 }} 个</el-descriptions-item>
          <el-descriptions-item label="置信度">{{ formatConfidence(issue.confidence) }}</el-descriptions-item>
          <el-descriptions-item label="证据等级">{{ evidenceQualityLabel(issue.evidence_quality) }}</el-descriptions-item>
          <el-descriptions-item label="风险分">{{ formatRiskScore(issue.risk_score) }}</el-descriptions-item>
          <el-descriptions-item label="冲突状态">
            <el-tag :type="issue.conflict_status === 'unresolved' ? 'warning' : 'success'" size="small">
              {{ conflictLabel(issue.conflict_status) }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="issue.source_details?.length" class="claim-list">
          <div v-for="claim in issue.source_details" :key="claim.claim_id || claim.source" class="claim-row">
            <span>{{ claim.agent_name || claim.source }}</span>
            <span>{{ claim.severity || '-' }} / {{ formatConfidence(claim.confidence) }}</span>
          </div>
        </div>
      </div>

      <div v-if="needsHumanReview" class="drawer-section review-panel">
        <div class="drawer-label">人工复核</div>
        <el-input
          v-model="reviewNote"
          type="textarea"
          :rows="3"
          maxlength="1000"
          show-word-limit
          placeholder="可选：记录接受、驳回或补充证据的依据"
        />
        <div class="review-actions">
          <el-button type="success" :loading="reviewing" @click="submitReview('accepted')">接受结论</el-button>
          <el-button type="danger" plain :loading="reviewing" @click="submitReview('rejected')">驳回结论</el-button>
          <el-button type="warning" plain :loading="reviewing" @click="submitReview('evidence_requested')">要求补充证据</el-button>
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-label">问题描述</div>
        <div class="drawer-content">{{ issue.description }}</div>
      </div>

      <!-- v3: CVSS 评分 + 向量字符串 -->
      <div class="drawer-section">
        <div class="drawer-label">CVSS 评分</div>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="评分">
            <el-tag v-if="verifiedCvssScore != null" size="small" :type="cvssTagType(verifiedCvssScore)" effect="dark">
              {{ verifiedCvssScore.toFixed(1) }}
            </el-tag>
            <span v-if="verifiedCvssScore != null" class="cvss-level-text">{{ cvssLevelText(verifiedCvssScore) }}</span>
            <span v-else class="cvss-level-text">未评分</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="verifiedCvssVector" label="向量">
            <code class="cvss-vector">{{ verifiedCvssVector }}</code>
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
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { formatDateTime } from '@/utils/format'
import { MagicStick } from '@element-plus/icons-vue'
import SeverityTag from './SeverityTag.vue'
import AiPromptModal from './AiPromptModal.vue'
import type { IssueOut, ComplianceMapping } from '@/types/review'
import { reviewDecision } from '@/api/issue'

const props = defineProps<{
  modelValue: boolean
  issue: IssueOut | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'reviewed', value: IssueOut): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (val) => emit('update:modelValue', val),
})

const title = computed(() => props.issue?.title ?? '问题详情')
const reviewNote = ref('')
const reviewing = ref(false)
const hasAggregation = computed(() => Boolean(
  props.issue?.aggregation_version
  || props.issue?.source_details?.length
  || props.issue?.conflict_status === 'unresolved',
))
const needsHumanReview = computed(() => (
  props.issue?.human_review_status === 'pending'
  || props.issue?.human_review_status === 'evidence_requested'
  || props.issue?.conflict_status === 'unresolved'
))

watch(() => props.issue?.id, () => { reviewNote.value = '' })

/** 仅信任后端标记为有效 v3.1 向量派生的分数。 */
const verifiedCvssScore = computed<number | null>(() => {
  const issue = props.issue
  const score = issue?.cvss_score
  if (
    issue?.cvss_source !== 'vector'
    || issue.cvss_version !== '3.1'
    || !issue.cvss_vector?.trim()
    || typeof score !== 'number'
    || !Number.isFinite(score)
    || score < 0
    || score > 10
  ) {
    return null
  }
  return score
})

const verifiedCvssVector = computed(() => (
  verifiedCvssScore.value === null ? null : props.issue?.cvss_vector?.trim() || null
))

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

function formatConfidence(value?: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value * 100)}%` : '未声明'
}

function formatRiskScore(value?: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '-'
}

function evidenceQualityLabel(value?: string | null): string {
  return ({ verified: '源码已核验', direct: '直接证据', inferred: '路径推断', unsupported: '证据不足' } as Record<string, string>)[value || ''] || '未评估'
}

function conflictLabel(value?: string | null): string {
  return ({ unresolved: '待复核', resolved: '已解决', none: '无冲突' } as Record<string, string>)[value || 'none'] || value || '无冲突'
}

async function submitReview(decision: 'accepted' | 'rejected' | 'evidence_requested'): Promise<void> {
  if (!props.issue || reviewing.value) return
  reviewing.value = true
  try {
    const updated = await reviewDecision(props.issue.id, { decision, note: reviewNote.value.trim() || undefined })
    ElMessage.success({ accepted: '已接受聚合结论', rejected: '已驳回聚合结论', evidence_requested: '已记录补充证据要求' }[decision])
    emit('reviewed', updated)
    reviewNote.value = ''
  } catch {
    ElMessage.error('复核决定保存失败，问题仍保持待复核，可稍后重试')
  } finally {
    reviewing.value = false
  }
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

.claim-list {
  margin-top: 10px;
  border: 1px solid var(--el-border-color-lighter);
}

.claim-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  font-size: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);

  &:last-child { border-bottom: 0; }
}

.review-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;

  :deep(.el-button + .el-button) { margin-left: 0; }
}
</style>
