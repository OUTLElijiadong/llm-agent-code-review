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
      <el-table-column prop="file_name" label="文件" min-width="160" show-overflow-tooltip />
      <el-table-column prop="line_number" label="行号" width="80" />
      <el-table-column prop="issue_type" label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ issueTypeLabel(row.issue_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="severity" label="严重度" width="90">
        <template #default="{ row }">
          <SeverityTag :severity="row.severity" />
        </template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="create_time" label="发现时间" width="160">
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
import { ref, reactive, watch } from 'vue'
import { formatDateTime } from '@/utils/format'
import SeverityTag from './SeverityTag.vue'
import { SEVERITY_OPTIONS } from '@/constants/severity'
import type { IssueOut } from '@/types/review'

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
</style>
