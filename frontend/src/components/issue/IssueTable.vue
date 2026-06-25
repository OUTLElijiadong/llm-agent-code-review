<template>
  <div class="issue-table-wrapper">
    <div class="issue-table-filters">
      <el-select v-model="localFilter.severity" placeholder="严重程度" clearable size="small" style="width: 120px" @change="onFilterChange">
        <el-option
          v-for="option in SEVERITY_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
      <el-select v-model="localFilter.issue_type" placeholder="问题类型" clearable size="small" style="width: 140px" @change="onFilterChange">
        <el-option label="代码规范" value="style" />
        <el-option label="安全漏洞" value="security" />
        <el-option label="性能问题" value="performance" />
        <el-option label="逻辑错误" value="logic" />
        <el-option label="可维护性" value="maintainability" />
      </el-select>
      <el-select v-model="localFilter.status" placeholder="状态" clearable size="small" style="width: 120px" @change="onFilterChange">
        <el-option label="未修复" value="unfixed" />
        <el-option label="已修复" value="fixed" />
        <el-option label="已忽略" value="ignored" />
        <el-option label="待审核" value="pending_review" />
      </el-select>
    </div>

    <el-table :data="issues" style="width: 100%" size="small" @row-click="onRowClick" highlight-current-row>
      <el-table-column prop="file_name" label="文件" min-width="140" show-overflow-tooltip />
      <el-table-column prop="line_number" label="行号" width="70" />
      <el-table-column prop="issue_type" label="类型" width="90">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ issueTypeLabel(row.issue_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="severity" label="严重度" width="80">
        <template #default="{ row }">
          <SeverityTag :severity="row.severity" />
        </template>
      </el-table-column>
      <!-- v3: CVSS 评分列(带颜色徽章) -->
      <el-table-column label="CVSS" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.cvss_score != null" size="small" :type="cvssTagType(row.cvss_score)" effect="dark">
            {{ row.cvss_score.toFixed(1) }}
          </el-tag>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <!-- v3: 合规映射列(命中的标准徽章) -->
      <el-table-column label="合规映射" width="160" align="center">
        <template #default="{ row }">
          <template v-if="row.compliance_mapping && hasComplianceHits(row.compliance_mapping)">
            <el-tag
              v-for="std in hitComplianceStandards(row.compliance_mapping)"
              :key="std.code"
              size="small"
              type="warning"
              effect="plain"
              class="compliance-badge"
            >
              {{ std.label }}
            </el-tag>
          </template>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <!-- v3: 来源列(LLM/静态/混合) -->
      <el-table-column label="来源" width="90" align="center">
        <template #default="{ row }">
          <span class="source-text">
            {{ sourceLabel(row.source) }}
            <el-tooltip
              v-if="row.static_rule_hits && row.static_rule_hits > 0"
              :content="`双引擎命中(静态规则命中 ${row.static_rule_hits} 次)`"
              placement="top"
            >
              <el-icon class="dual-engine-icon"><Aim /></el-icon>
            </el-tooltip>
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="create_time" label="发现时间" width="150">
        <template #default="{ row }">
          {{ formatDateTime(row.create_time) }}
        </template>
      </el-table-column>
    </el-table>

    <div v-if="showPagination" class="issue-table-pagination">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        small
        @change="onPageChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 问题列表表格组件
 * v3 增强:
 *  - 新增 CVSS 评分列(带颜色徽章:9-10红/7-8.9橙/4-6.9黄/0-3.9蓝)
 *  - 新增 合规映射列(显示命中的标准徽章,如 ISO/GDPR/PCI)
 *  - 新增 来源列(LLM/静态/混合 对应中文标签)
 *  - 双引擎命中(static_rule_hits > 0)显示标记图标
 *  - 保持原有列(行号/类型/严重度/标题/状态/发现时间)不变
 */
import { ref, reactive, watch } from 'vue'
import { Aim } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'
import SeverityTag from './SeverityTag.vue'
import { SEVERITY_OPTIONS } from '@/constants/severity'
import type { IssueOut, ComplianceMapping } from '@/types/review'

const props = withDefaults(defineProps<{
  issues: IssueOut[]
  total?: number
  showPagination?: boolean
}>(), {
  total: 0,
  showPagination: true,
})

const emit = defineEmits<{
  (e: 'row-click', issue: IssueOut): void
  (e: 'filter-change', filter: Record<string, string>): void
  (e: 'page-change', page: number, pageSize: number): void
}>()

const currentPage = ref(1)
const pageSize = ref(50)

const localFilter = reactive({
  severity: '',
  issue_type: '',
  status: '',
})

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
  { code: 'iso27001', label: 'ISO' },
  { code: 'gdpr', label: 'GDPR' },
  { code: 'pci_dss', label: 'PCI' },
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
 * @param source - 来源字段值(LLM/静态/混合)
 * @returns 中文标签
 */
function sourceLabel(source?: string): string {
  if (!source) return '-'
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
 * 返回命中的合规标准列表(用于徽章展示)
 * v3 新增
 * @param mapping - 合规映射对象
 * @returns 命中的标准元信息数组
 */
function hitComplianceStandards(mapping: ComplianceMapping): { code: string; label: string }[] {
  return complianceStandardMeta
    .filter((meta) => (mapping[meta.code]?.length ?? 0) > 0)
    .map((meta) => ({ code: meta.code, label: meta.label }))
}

/**
 * 将本地筛选条件同步给父组件
 * @returns void
 */
function onFilterChange(): void {
  emit('filter-change', { ...localFilter })
}

/**
 * 将分页变化同步给父组件
 * @returns void
 */
function onPageChange(): void {
  emit('page-change', currentPage.value, pageSize.value)
}

/**
 * 将当前点击的问题行同步给父组件
 * @param row - 当前点击的问题
 * @returns void
 */
function onRowClick(row: IssueOut): void {
  emit('row-click', row)
}

watch(() => props.total, () => {
  currentPage.value = 1
})
</script>

<style scoped lang="scss">
.issue-table-wrapper {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.issue-table-filters {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.issue-table-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
}

.text-muted {
  color: var(--el-text-color-placeholder);
}

/* v3: 合规映射徽章间距 */
.compliance-badge {
  margin-right: 4px;

  &:last-child {
    margin-right: 0;
  }
}

/* v3: 来源单元格样式 */
.source-text {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}

.dual-engine-icon {
  color: var(--el-color-warning);
  font-size: 14px;
}
</style>
