<template>
  <div class="system-audit-page">
    <div class="page-header">
      <div>
        <h2>系统操作审计</h2>
        <p class="page-sub">
          关键操作流水：登录、用户管理、规则变更、Agent 调用、项目变更
        </p>
      </div>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-select v-model="filters.action" placeholder="操作类型" clearable style="width: 160px" @change="reload">
          <el-option label="登录" value="login" />
          <el-option label="用户管理" value="user" />
          <el-option label="规则变更" value="rule" />
          <el-option label="Agent 调用" value="ai" />
          <el-option label="项目变更" value="project" />
        </el-select>
        <el-input
          v-model="filters.keyword"
          placeholder="搜索操作描述或操作者"
          clearable
          style="width: 240px"
          @change="reload"
        />
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
          @change="reload"
        />
      </div>

      <el-table v-loading="loading" :data="rows" stripe empty-text="暂无审计记录">
        <el-table-column prop="id" label="日志ID" width="90" />
        <el-table-column prop="create_time" label="时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.create_time) }}</template>
        </el-table-column>
        <el-table-column prop="actor_name" label="操作者" width="160">
          <template #default="{ row }">
            <div>{{ row.actor_name || '系统' }}</div>
            <div class="trace-sub">{{ row.actor_id ? `用户 #${row.actor_id}` : 'system' }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="action" label="操作类型" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="actionTagType(row.action)">{{ actionLabel(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="对象" width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.target_type">{{ row.target_type }} · {{ row.target_id }}</span>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="说明" min-width="220" show-overflow-tooltip />
        <el-table-column prop="ip" label="来源" width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.ip || '-' }}</template>
        </el-table-column>
        <el-table-column prop="status" label="结果" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'success' ? 'success' : 'danger'">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="追溯" width="110" fixed="right">
          <template #default="{ row }">
            <el-button v-if="traceRoute(row)" link type="primary" @click="goTrace(row)">查看</el-button>
            <span v-else class="text-muted">已记录</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadLogs"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { formatDateTime } from '@/utils/format'
import { listAuditLogs } from '@/api/audit'
import type { AuditLogOut } from '@/types/audit'

const router = useRouter()
const loading = ref(false)
const rows = ref<AuditLogOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const filters = reactive({
  action: '',
  keyword: '',
})
const dateRange = ref<[string, string] | null>(null)

function actionLabel(action: string): string {
  const map: Record<string, string> = {
    login: '登录',
    user: '用户管理',
    rule: '规则变更',
    ai: 'Agent 调用',
    project: '项目变更',
    agent: 'Agent 配置',
  }
  return map[action] ?? action
}

function actionTagType(action: string): 'success' | 'warning' | 'danger' | 'info' | '' {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info' | ''> = {
    login: '',
    user: 'warning',
    rule: 'info',
    ai: 'success',
    project: 'info',
    agent: 'warning',
  }
  return map[action] ?? ''
}

async function loadLogs(): Promise<void> {
  loading.value = true
  try {
    const data = await listAuditLogs({
      action: filters.action || undefined,
      keyword: filters.keyword || undefined,
      start: dateRange.value?.[0],
      end: dateRange.value?.[1],
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function reload(): void {
  page.value = 1
  loadLogs()
}

/**
 * 根据审计对象类型计算可追溯页面。
 * @param row - 审计日志行
 * @returns 可跳转路由;无法定位单页时返回空字符串
 */
function traceRoute(row: AuditLogOut): string {
  const id = row.target_id
  if (row.target_type === 'project' && id) return `/projects/${id}`
  if (row.target_type === 'user') return '/admin/users'
  if (row.target_type === 'rule') return '/rules'
  if (row.target_type === 'proposal' || row.target_type === 'evolution') return '/admin/evolution'
  return ''
}

/**
 * 跳转到审计对象对应页面。
 * @param row - 审计日志行
 * @returns void
 */
function goTrace(row: AuditLogOut): void {
  const target = traceRoute(row)
  if (target) router.push(target)
}


onMounted(loadLogs)
</script>

<style scoped lang="scss">
.system-audit-page {
  padding: var(--spacing-lg);
}

.page-header {
  margin-bottom: var(--spacing-lg);

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  align-items: center;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.text-muted {
  color: var(--color-text-secondary, #909399);
}

.trace-sub {
  color: var(--color-text-secondary, #909399);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  line-height: 1.4;
}
</style>
