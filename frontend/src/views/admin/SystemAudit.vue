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

      <div v-loading="loading" class="audit-cards" role="list" data-testid="audit-cards">
        <EmptyState v-if="!rows.length" description="暂无审计记录" />
        <article
          v-for="row in rows"
          :key="row.id"
          class="audit-card"
          :class="{ 'is-open': expandedId === row.id }"
          :data-status="row.status"
          role="listitem"
          tabindex="0"
          :aria-expanded="expandedId === row.id ? 'true' : 'false'"
          @click="toggleExpand(row.id)"
          @keydown.enter.prevent="toggleExpand(row.id)"
          @keydown.space.prevent="toggleExpand(row.id)"
        >
          <span class="ac-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="ac-main">
            <div class="ac-line1">
              <el-tag size="small" :type="actionTagType(row.action)">{{ actionLabel(row.action) }}</el-tag>
              <b class="ac-actor">{{ row.actor_name || '系统' }}</b>
              <el-tag class="ac-status" size="small" :type="row.status === 'success' ? 'success' : 'danger'">
                {{ row.status === 'success' ? '成功' : '失败' }}
              </el-tag>
            </div>
            <div class="ac-line2 font-mono">
              <span>{{ formatDateTime(row.create_time) }}</span>
              <span>#{{ row.id }}</span>
              <span>{{ row.actor_id ? `用户 #${row.actor_id}` : 'system' }}</span>
            </div>
          </div>
          <el-icon class="ac-chevron" :class="{ 'is-open': expandedId === row.id }" aria-hidden="true">
            <ArrowDown />
          </el-icon>
          <div v-if="expandedId === row.id" class="ac-detail" @click.stop>
            <div class="ac-row">
              <span class="ac-label">对象</span>
              <span v-if="row.target_type" class="font-mono">{{ row.target_type }} · {{ row.target_id }}</span>
              <span v-else class="text-muted">-</span>
            </div>
            <div class="ac-row">
              <span class="ac-label">说明</span>
              <span class="ac-text" :title="row.detail || ''">{{ row.detail || '-' }}</span>
            </div>
            <div class="ac-row">
              <span class="ac-label">来源</span>
              <span class="font-mono">{{ row.ip || '-' }}</span>
            </div>
            <div class="ac-row">
              <span class="ac-label">追溯</span>
              <el-button v-if="traceRoute(row)" link type="primary" size="small" @click="goTrace(row)">查看</el-button>
              <span v-else class="text-muted">已记录</span>
            </div>
          </div>
        </article>
      </div>

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
import { ArrowDown } from '@element-plus/icons-vue'
import EmptyState from '@/components/common/EmptyState.vue'
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

/** 当前展开详情的日志 ID(null = 全部收起)。 */
const expandedId = ref<number | null>(null)

function toggleExpand(id: number): void {
  expandedId.value = expandedId.value === id ? null : id
}

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
  if (row.target_type === 'rule') return '/security?tab=rules'
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

/* ── 审计卡片列表(替代表格:摘要行点击展开详情,说明全文不再截断) ── */
.audit-cards {
  display: grid;
  gap: 10px;
  min-height: 120px;
}
.audit-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 4px 14px;
  align-items: center;
  padding: 12px 16px 12px 12px;
  border-radius: 12px;
  background: #fff;
  border: 1px solid var(--gray-100, #eef0f4);
  cursor: pointer;
  transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.audit-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.audit-card.is-open {
  border-color: var(--brand-300, #a8c4fa);
  box-shadow: 0 6px 16px rgba(23, 34, 62, .06);
}
.audit-card:focus-visible {
  outline: 2px solid var(--brand-400, #6f9df7);
  outline-offset: 2px;
}
.ac-band {
  width: 4px;
  height: 38px;
  border-radius: 999px;
  background: var(--sev-severe, #dc4961);
}
.ac-band[data-status='success'] {
  background: #40a35f;
}
.ac-main {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.ac-line1 {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ac-actor {
  font-size: 13.5px;
  font-weight: 600;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ac-status {
  margin-left: auto;
}
.ac-line2 {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--color-text-secondary, #909399);
}
.ac-chevron {
  color: var(--color-text-secondary, #909399);
  font-size: 13px;
  transition: transform .18s ease, color .18s ease;
}
.ac-chevron.is-open {
  transform: rotate(180deg);
  color: var(--brand-500, #4078f4);
}
.ac-detail {
  grid-column: 1 / -1;
  display: grid;
  gap: 8px;
  margin-top: 6px;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--el-fill-color-light, #f5f7fa);
}
.ac-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12.5px;
}
.ac-label {
  flex: none;
  width: 32px;
  color: var(--color-text-secondary, #909399);
  font-size: 11.5px;
}
.ac-text {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow: auto;
}

@media (prefers-reduced-motion: reduce) {
  .audit-card,
  .ac-chevron {
    transition: none;
  }
  .audit-card:hover {
    transform: none;
  }
}
@media (max-width: 760px) {
  .audit-card {
    padding: 10px 12px 10px 10px;
    gap: 4px 10px;
  }
  .ac-actor {
    max-width: 46vw;
  }
  .ac-status {
    margin-left: 0;
  }
  .ac-line2 {
    gap: 10px;
  }
}
</style>
