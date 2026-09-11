<template>
  <div class="ai-log-list-page">
    <div class="page-header">
      <h2>AI调用日志</h2>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-select v-model="filterStatus" placeholder="调用状态" clearable style="width: 120px" @change="loadData">
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
          @change="loadData"
        />
        <!-- 原 el-table 各列的本页内排序能力,改为筛选栏排序控件(仍为当前页客户端排序) -->
        <el-select v-model="sortField" placeholder="排序" clearable style="width: 130px">
          <el-option label="按调用时间" value="create_time" />
          <el-option label="按输入Token" value="prompt_tokens" />
          <el-option label="按输出Token" value="completion_tokens" />
          <el-option label="按总Token" value="total_tokens" />
          <el-option label="按耗时" value="duration_ms" />
        </el-select>
        <el-select v-if="sortField" v-model="sortOrder" style="width: 96px">
          <el-option label="降序" value="desc" />
          <el-option label="升序" value="asc" />
        </el-select>
      </div>

      <div class="log-cards" v-loading="loading" role="list" data-testid="log-cards">
        <EmptyState
          v-if="!logs.length"
          compact
          description="当前过滤条件下没有调用记录,试试放宽条件"
        />
        <article
          v-for="row in displayLogs"
          :key="row.id"
          class="log-card"
          :class="{ expanded: expandedId === row.id }"
          :data-status="row.status"
          :data-testid="'log-card-' + row.id"
          role="listitem"
          @click="toggleExpand(row)"
        >
          <span class="tc-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="tc-main">
            <div class="tc-line1">
              <b class="tc-title">调用 #{{ row.id }}</b>
              <span class="tc-model font-mono" :title="row.model_name">{{ row.model_name }}</span>
              <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                {{ row.status === 'success' ? '成功' : '失败' }}
              </el-tag>
            </div>
            <div class="tc-line2 font-mono">
              <span class="tc-meta">
                <el-button
                  v-if="row.project_id"
                  link
                  type="primary"
                  size="small"
                  class="tc-link"
                  @click.stop="goProject(row.project_id)"
                >{{ row.project_name || `项目 #${row.project_id}` }}</el-button>
                <template v-else>项目 -</template>
              </span>
              <span class="tc-meta">
                <el-button
                  v-if="row.task_id"
                  link
                  type="primary"
                  size="small"
                  class="tc-link"
                  @click.stop="goTask(row.task_id)"
                >{{ row.task_name || `任务 #${row.task_id}` }}</el-button>
                <template v-else>任务 -</template>
              </span>
              <span class="tc-meta">
                <el-button
                  v-if="row.project_id && row.file_id"
                  link
                  type="primary"
                  size="small"
                  class="tc-link"
                  @click.stop="goFile(row.project_id, row.file_id)"
                >{{ row.file_name || `文件 #${row.file_id}` }}</el-button>
                <template v-else>{{ row.file_name || (row.file_id ? `文件 #${row.file_id}` : '文件 -') }}</template>
              </span>
              <span class="tc-meta">用户 {{ row.user_name || (row.user_id ? `#${row.user_id}` : '-') }}</span>
              <span class="tc-meta">分片 {{ row.chunk_index ?? '-' }}</span>
              <span class="tc-meta">{{ formatDateTime(row.create_time) }}</span>
            </div>
          </div>
          <div
            class="tc-metrics font-mono"
            :title="`输入 ${row.prompt_tokens ?? '-'} / 输出 ${row.completion_tokens ?? '-'} Token`"
          >
            <span class="tc-tokens">总Token {{ row.total_tokens ?? '—' }}</span>
            <span class="tc-duration">{{ formatDuration(row.duration_ms) }}</span>
          </div>
          <button
            type="button"
            class="tc-chevron"
            :class="{ open: expandedId === row.id }"
            :aria-expanded="expandedId === row.id"
            :aria-label="expandedId === row.id ? `收起调用 ${row.id} 详情` : `展开调用 ${row.id} 详情`"
            @click.stop="toggleExpand(row)"
          >
            <el-icon><ArrowDown /></el-icon>
          </button>

          <div v-if="expandedId === row.id" class="tc-detail" @click.stop>
            <div v-if="detailLoading && !activeDetail" class="tc-detail-loading">正在读取日志详情…</div>
            <template v-else-if="activeDetail">
              <div class="tc-detail-grid font-mono">
                <div class="td-item"><span class="td-k">日志ID</span><span class="td-v">{{ activeDetail.id }}</span></div>
                <div class="td-item"><span class="td-k">模型</span><span class="td-v">{{ activeDetail.model_name }}</span></div>
                <div class="td-item">
                  <span class="td-k">状态</span>
                  <span class="td-v">
                    <el-tag :type="activeDetail.status === 'success' ? 'success' : 'danger'" size="small">
                      {{ activeDetail.status === 'success' ? '成功' : '失败' }}
                    </el-tag>
                  </span>
                </div>
                <div class="td-item"><span class="td-k">输入Token</span><span class="td-v">{{ activeDetail.prompt_tokens ?? '-' }}</span></div>
                <div class="td-item"><span class="td-k">输出Token</span><span class="td-v">{{ activeDetail.completion_tokens ?? '-' }}</span></div>
                <div class="td-item"><span class="td-k">总Token</span><span class="td-v">{{ activeDetail.total_tokens ?? '-' }}</span></div>
                <div class="td-item"><span class="td-k">耗时</span><span class="td-v">{{ formatDuration(activeDetail.duration_ms) }}</span></div>
                <div class="td-item"><span class="td-k">分片</span><span class="td-v">{{ activeDetail.chunk_index ?? '-' }}</span></div>
                <div class="td-item">
                  <span class="td-k">用户</span>
                  <span class="td-v">{{ activeDetail.user_name || (activeDetail.user_id ? `#${activeDetail.user_id}` : '-') }}</span>
                </div>
                <div class="td-item"><span class="td-k">调用时间</span><span class="td-v">{{ formatDateTime(activeDetail.create_time) }}</span></div>
              </div>

              <div class="tc-trace">
                <span class="td-k">追溯</span>
                <el-button v-if="activeDetail.project_id" link type="primary" size="small" @click="goProject(activeDetail.project_id)">
                  项目
                </el-button>
                <el-button v-if="activeDetail.task_id" link type="primary" size="small" @click="goTask(activeDetail.task_id)">
                  任务
                </el-button>
                <el-button
                  v-if="activeDetail.project_id && activeDetail.file_id"
                  link
                  type="primary"
                  size="small"
                  @click="goFile(activeDetail.project_id, activeDetail.file_id)"
                >
                  文件
                </el-button>
                <span v-if="!activeDetail.project_id && !activeDetail.task_id && !(activeDetail.project_id && activeDetail.file_id)" class="text-muted">仅日志</span>
              </div>

              <div v-if="activeDetail.error_message" class="detail-section">
                <div class="detail-label">错误信息</div>
                <div class="detail-content error-content">{{ activeDetail.error_message }}</div>
              </div>

              <div v-if="activeDetail.prompt" class="detail-section">
                <div class="detail-label">
                  <span>请求Prompt</span>
                  <el-button link type="primary" size="small" @click="copyText(activeDetail.prompt || '')">复制</el-button>
                </div>
                <div class="detail-code">
                  <pre><code>{{ activeDetail.prompt }}</code></pre>
                </div>
              </div>

              <div v-if="activeDetail.response" class="detail-section">
                <div class="detail-label">
                  <span>AI响应</span>
                  <el-button link type="primary" size="small" @click="copyText(activeDetail.response || '')">复制</el-button>
                </div>
                <div class="detail-code">
                  <pre><code>{{ activeDetail.response }}</code></pre>
                </div>
              </div>
            </template>
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
          @change="loadData"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowDown } from '@element-plus/icons-vue'

import EmptyState from '@/components/common/EmptyState.vue'
import { getAiLogs, getAiLogDetail } from '@/api/aiLog'
import type { AiLogOut, AiLogDetailOut } from '@/types/aiLog'
import { formatDateTime } from '@/utils/format'
import { ElMessage } from 'element-plus/es/components/message/index'

const router = useRouter()
const loading = ref(false)
const logs = ref<AiLogOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterStatus = ref('')
const dateRange = ref<[string, string] | null>(null)

/** 本页内排序(承接原 el-table 各列 sortable 的客户端排序语义)。 */
type SortField = 'prompt_tokens' | 'completion_tokens' | 'total_tokens' | 'duration_ms' | 'create_time'
const sortField = ref<SortField | ''>('')
const sortOrder = ref<'asc' | 'desc'>('desc')

const displayLogs = computed<AiLogOut[]>(() => {
  const field = sortField.value
  if (!field) return logs.value
  const dir = sortOrder.value === 'asc' ? 1 : -1
  return [...logs.value].sort((a, b) => {
    const av = a[field]
    const bv = b[field]
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    if (av === bv) return 0
    return (av > bv ? 1 : -1) * dir
  })
})

/** 展开态:卡片点击展开详情,详情数据复用原 getAiLogDetail,按日志 ID 缓存。 */
const expandedId = ref<number | null>(null)
const detailCache = ref<Record<number, AiLogDetailOut>>({})
const detailLoading = ref(false)
let detailRequest = 0

const activeDetail = computed<AiLogDetailOut | null>(() =>
  expandedId.value != null ? detailCache.value[expandedId.value] ?? null : null
)

async function toggleExpand(row: AiLogOut) {
  if (expandedId.value === row.id) {
    expandedId.value = null
    return
  }
  expandedId.value = row.id
  if (detailCache.value[row.id]) return
  const request = ++detailRequest
  detailLoading.value = true
  try {
    const data = await getAiLogDetail(row.id)
    if (request !== detailRequest) return
    detailCache.value = { ...detailCache.value, [row.id]: data }
  } catch {
    if (request !== detailRequest) return
    ElMessage.error('获取日志详情失败')
    if (expandedId.value === row.id) expandedId.value = null
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

function formatDuration(ms?: number): string {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filterStatus.value) params.status = filterStatus.value
    if (dateRange.value) {
      params.start = dateRange.value[0]
      params.end = dateRange.value[1]
    }

    const data = await getAiLogs(params)
    logs.value = data.items
    total.value = data.total
    // 翻页/筛选后当前展开的日志若已不在列表中,收起展开区
    if (expandedId.value != null && !data.items.some((item) => item.id === expandedId.value)) {
      expandedId.value = null
    }
  } finally {
    loading.value = false
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败')
  }
}

/**
 * 跳转到项目详情页。
 * @param projectId - 项目 ID
 * @returns void
 */
function goProject(projectId: number): void {
  router.push(`/projects/${projectId}`)
}

/**
 * 跳转到审查任务详情页。
 * @param taskId - 审查任务 ID
 * @returns void
 */
function goTask(taskId: number): void {
  router.push(`/reviews/${taskId}`)
}

/**
 * 跳转到代码文件编辑页。
 * @param projectId - 文件所属项目 ID
 * @param fileId - 代码文件 ID
 * @returns void
 */
function goFile(projectId: number, fileId: number): void {
  router.push(`/code/${projectId}/file/${fileId}`)
}

onMounted(() => {
  loadData()
})
</script>

<style scoped lang="scss">
.ai-log-list-page {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* ── 日志卡片列表(替代表格:摘要行突出模型/状态/Token/耗时,详情点击展开) ── */
.log-cards {
  display: grid;
  gap: 10px;
  min-height: 120px;
}

.log-card {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: 14px;
  align-items: center;
  padding: 13px 16px 13px 12px;
  border-radius: 12px;
  background: #fff;
  border: 1px solid var(--gray-100, #eef0f4);
  cursor: pointer;
  transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
}

.log-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, 0.07);
  border-color: var(--brand-300, #a8c4fa);
}

/* 失败调用视觉锚点:左侧红条(band)+ 极浅红底,扫读时一眼定位 */
.log-card[data-status='failed'] {
  background: rgba(220, 73, 97, 0.035);
}

.log-card.expanded {
  border-color: var(--brand-300, #a8c4fa);
  box-shadow: 0 6px 18px rgba(23, 34, 62, 0.06);
}

.tc-band {
  width: 4px;
  height: 38px;
  border-radius: 999px;
}

.tc-band[data-status='success'] {
  background: #40a35f;
}

.tc-band[data-status='failed'] {
  background: var(--sev-severe, #dc4961);
}

.tc-main {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.tc-line1 {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.tc-title {
  font-size: 13.5px;
  font-weight: 600;
}

.tc-model {
  padding: 1px 8px;
  border: 1px solid var(--gray-200, #e3e6eb);
  border-radius: 6px;
  font-size: 11px;
  color: var(--gray-600, #4e5769);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tc-line2 {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--gray-500, #8a93a5);
}

.tc-meta {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tc-link {
  font-size: 11px;
  height: auto;
  padding: 0;
}

.tc-metrics {
  display: grid;
  gap: 3px;
  justify-items: end;
  text-align: right;
  min-width: 92px;
}

.tc-tokens {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--gray-700, #3b4256);
}

.tc-duration {
  font-size: 11px;
  color: var(--gray-500, #8a93a5);
}

.tc-chevron {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--gray-400, #b6bdc9);
  cursor: pointer;
  transition: transform 0.18s ease, color 0.18s ease;
}

.tc-chevron:hover {
  color: var(--brand-500, #4078f4);
}

.tc-chevron.open {
  transform: rotate(180deg);
  color: var(--brand-500, #4078f4);
}

/* ── 展开详情:三段Token分解/分片/追溯/原始报文 ── */
.tc-detail {
  grid-column: 1 / -1;
  margin-top: 2px;
  padding-top: 12px;
  border-top: 1px dashed var(--gray-200, #e3e6eb);
  display: grid;
  gap: 12px;
  cursor: default;
}

.tc-detail-loading {
  font-size: 12px;
  color: var(--gray-500, #8a93a5);
}

.tc-detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 8px 18px;
}

.td-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.td-k {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--gray-400, #b6bdc9);
}

.td-v {
  font-size: 12px;
  color: var(--gray-700, #3b4256);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tc-trace {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.detail-section {
  margin-top: 4px;
}

.detail-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 8px;
}

.detail-content {
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;

  &.error-content {
    color: #f56c6c;
    white-space: pre-wrap;
    word-break: break-all;
  }
}

.detail-code {
  background: #1e1e1e;
  border-radius: 4px;
  padding: 12px;
  max-height: 350px;
  overflow: auto;

  pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;

    code {
      color: #d4d4d4;
      font-size: 12px;
      line-height: 1.6;
      font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    }
  }
}

@media (prefers-reduced-motion: reduce) {
  .log-card,
  .tc-chevron {
    transition: none;
  }
}

@media (max-width: 760px) {
  .log-card {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .tc-band,
  .tc-chevron {
    display: none;
  }

  .tc-line2 {
    gap: 8px;
  }

  .tc-meta {
    max-width: 44vw;
  }

  .tc-detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
