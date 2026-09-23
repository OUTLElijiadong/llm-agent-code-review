<template>
  <div class="review-detail-page">
    <!-- ============ 页面加载中 ============ -->
    <PrismLoading
      v-if="pageLoading"
      label="正在加载审查详情"
      sublabel="正在请求审查数据与问题列表"
      overlay
    />

    <!-- ============ 加载失败 ============ -->
    <div v-else-if="pageError" class="page-error" role="alert">
      <EmptyState :description="pageError" />
      <p class="recovery-hint">{{ detailNextAction || '可重新读取原任务状态，或返回审查记录列表；重新加载不会再次发起审查。' }}</p>
      <p v-if="detailRequestId" class="request-id">请求编号：{{ detailRequestId }}</p>
      <div class="recovery-actions">
        <el-button type="primary" :icon="RefreshRight" :loading="refreshing" :disabled="refreshing" @click="loadAllData">
          重新加载
        </el-button>
        <el-button @click="router.push('/reviews')">返回审查记录</el-button>
      </div>
    </div>

    <!-- ============ 正常内容 ============ -->
    <template v-else>
    <section v-if="task" class="execution-panel" aria-label="执行阶段与覆盖">
      <div class="execution-status" role="status" aria-live="polite" aria-atomic="true">
        <div class="execution-heading">
          <strong>{{ stageLabel }}</strong>
          <span v-if="!isSandboxReport || task.model_name">{{ modelLabel }}</span>
          <span v-if="detailError" class="stale-note">上次成功获取的数据；当前连接已中断</span>
          <span v-else-if="refreshing">正在获取最新状态…</span>
        </div>
        <dl v-if="isSandboxReport" class="coverage-grid">
          <div><dt>已处理范围 / 总范围</dt><dd>{{ coverageCount(task.processed_files) }} / {{ coverageCount(task.total_files) }}</dd></div>
        </dl>
        <dl v-else class="coverage-grid">
          <div><dt>当前文件</dt><dd>{{ task.coverage?.current_file || '未知（接口未提供）' }}</dd></div>
          <div><dt>已完成文件 / 总文件</dt><dd>{{ coverageCount(task.coverage?.completed_files) }} / {{ coverageCount(task.coverage?.total_files) }}</dd></div>
          <div><dt>当前 / 最近文件分片</dt><dd>{{ coverageCount(task.coverage?.completed_chunks) }} / {{ coverageCount(task.coverage?.total_chunks) }}</dd></div>
        </dl>
        <p v-if="isSandboxReport" class="coverage-note">沙箱测试按任务范围记录完成情况；逐文件、分片和模型信息以报告中已保存的证据为准。</p>
        <p v-else class="coverage-note">仅展示服务端返回的执行记录；分片数不是全任务合计，未知字段不推算为进度。</p>
      </div>
      <div v-if="taskFailure" class="execution-error" role="alert">{{ taskFailure }}</div>
    </section>

    <div v-if="detailError" class="connection-error" role="alert">
      <strong>状态更新失败：{{ detailError }}</strong>
      <p>已暂停自动刷新。重新获取只读取原任务状态，不会重新发起审查。</p>
      <p v-if="detailRequestId" class="request-id">请求编号：{{ detailRequestId }}</p>
      <el-button :loading="refreshing" :disabled="refreshing" @click="loadAllData">{{ refreshing ? '正在重新连接' : '重新获取状态' }}</el-button>
    </div>

    <!-- ============ 顶部摘要条 ============ -->
    <header class="task-head">
      <el-button link class="back-btn" @click="goBack(router, '/reviews')">
        <el-icon><ArrowLeft /></el-icon>返回
      </el-button>

      <div class="head-title">
        <h1 class="font-display">{{ task?.task_name || `审查任务 #${taskId}` }}</h1>
        <div class="head-meta font-mono">
          <span>{{ reviewTypeLabel(task?.review_type) }}</span>
          <template v-if="!isSandboxReport || task?.model_name"><span class="dot">·</span>
          <span>{{ modelLabel }}</span></template>
          <span class="dot">·</span>
          <span>{{ formatDuration(Number(task?.duration_ms ?? 0)) }}</span>
        </div>
        <div class="trace-meta">
          <span class="font-mono">任务 #{{ taskId }}</span>
          <el-button v-if="task?.project_id" link type="primary" @click="goProject(task.project_id)">
            {{ task.project_name || `项目 #${task.project_id}` }}
          </el-button>
          <el-button v-if="task?.status === 'success'" link type="primary" @click="goReport(taskId)">报告 #{{ taskId }}</el-button>
          <el-tag v-for="agent in task?.agent_releases || []" :key="agent.release_id" size="small" type="success" effect="plain">
            {{ agent.agent_name }} v{{ agent.agent_version }}
          </el-tag>
        </div>
      </div>

      <div v-if="displayScore !== null" class="head-score">
        <div class="score-orb" :style="{ background: scoreGradient(displayScore) }">
          <span class="score-val font-display">{{ displayScore }}</span>
          <span class="score-out font-mono">/100</span>
        </div>
        <div class="score-meta">
          <div class="score-label">{{ isTestScore ? '测试评分' : '代码质量' }}</div>
          <div class="score-status" :style="{ color: scoreFlatColor(displayScore) }">{{ riskLevel }}</div>
        </div>
      </div>
      <div v-else class="score-unavailable">{{ task?.status === 'success' ? '评分未知（接口未提供有效评分）' : '尚无最终评分' }}</div>

      <div class="head-tally">
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-severe)' }">{{ tallyCount('严重', task?.severe_issues) }}</span>
          <span class="t-label">危急</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-high)' }">{{ tallyCount('高', task?.high_issues) }}</span>
          <span class="t-label">高</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-medium)' }">{{ tallyCount('中', task?.medium_issues) }}</span>
          <span class="t-label">中</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-low)' }">{{ tallyCount('低', task?.low_issues) }}</span>
          <span class="t-label">低</span>
        </div>
        <div v-if="isSandboxReport && task?.report_issue_summary?.unclassified" class="tally-item">
          <span class="t-val font-display">{{ task.report_issue_summary.unclassified }}</span><span class="t-label">未分级</span>
        </div>
        <div class="tally-divider"></div>
        <div class="tally-item">
          <span class="t-val font-display">{{ isSandboxReport ? (task?.report_issue_summary?.total ?? '—') : (task?.total_issues ?? 0) }}</span>
          <span class="t-label">{{ isSandboxReport ? '报告条目' : '总计' }}</span>
        </div>
      </div>

      <div class="head-actions">
        <el-button
          v-if="task?.severe_issues || task?.high_issues || task?.medium_issues"
          :icon="Lock"
          size="small"
          type="danger"
          plain
          @click="securityScanVisible = true"
        >
          🛡 安全复审
        </el-button>
        <el-button
          v-if="!isSandboxReport && task?.total_issues && task.total_issues > 0"
          :icon="MagicStick"
          size="small"
          type="primary"
          plain
          @click="aiPromptVisible = true"
        >
          AI 修复包
        </el-button>
        <span class="status-pill" :class="`s-${task?.status ?? 'pending'}`">
          <span class="pill-dot"></span>{{ statusLabel(task?.status ?? '') }}
        </span>
      </div>
    </header>

    <section v-if="isSandboxReport" class="coverage-note" role="note">
      <p v-if="task?.report_issue_summary?.total != null">按报告“问题清单”的独立条目统计，严重度仅采用报告明示标签；条目数不代表已确认漏洞数。</p>
      <p v-else>报告未保存可识别的问题清单，条目数与严重度未确定。</p>
      <p v-if="!task?.report_issue_summary?.structured_issues">该报告没有结构化问题明细，请在报告中查看发现、证据和修复建议。</p>
    </section>

    <section v-if="task?.aggregation_summary?.aggregated" class="trust-strip" aria-label="可信聚合状态">
      <span class="trust-title">可信聚合</span>
      <span>已归一 {{ task.aggregation_summary.aggregated }}</span>
      <span>多源确认 {{ task.aggregation_summary.independently_confirmed }}</span>
      <span :class="{ attention: task.aggregation_summary.unresolved_conflicts > 0 }">
        冲突 {{ task.aggregation_summary.unresolved_conflicts }}
      </span>
      <span :class="{ attention: task.aggregation_summary.insufficient_evidence > 0 }">
        证据不足 {{ task.aggregation_summary.insufficient_evidence }}
      </span>
      <el-button
        v-if="task.aggregation_summary.pending_human_review > 0"
        type="warning"
        link
        @click="showPendingReviews"
      >
        待人工复核 {{ task.aggregation_summary.pending_human_review }}
      </el-button>
    </section>

    <AiPromptModal
      v-model="aiPromptVisible"
      source="task"
      :ref-id="taskId"
    />

    <SecurityScanModal
      v-model="securityScanVisible"
      source="task"
      :ref-id="taskId"
      :auto-start="true"
    />

    <!-- ============ 三栏工作台 ============ -->
    <section v-if="!isSandboxReport || task?.report_issue_summary?.structured_issues" class="workbench">
      <!-- 左：文件树 -->
      <aside class="pane pane-files">
        <header class="pane-head">
          <span class="font-display">文件列表</span>
          <span class="font-mono pane-count">{{ fileList.length }}</span>
        </header>
        <div class="pane-body">
          <div
            v-for="f in fileList"
            :key="f.file_id"
            class="file-row"
            role="button"
            tabindex="0"
            :aria-pressed="currentFileId === f.file_id"
            :class="{ active: currentFileId === f.file_id }"
            @click="onFilePick(f.file_id)"
            @keydown.enter.self.prevent="onFilePick(f.file_id)"
            @keydown.space.self.prevent="onFilePick(f.file_id)"
          >
            <span class="file-ico font-mono">{{ fileGlyph(f.file_name) }}</span>
            <span class="file-name">{{ f.file_name }}</span>
            <span v-if="fileIssueCount(f.file_id) > 0" class="file-issues" :style="{ color: scoreFlatColor(80 - fileIssueCount(f.file_id) * 5) }">
              {{ fileIssueCount(f.file_id) }}
            </span>
            <el-button
              v-if="f.project_id"
              class="file-open"
              link
              size="small"
              type="primary"
              title="编辑当前版本，内容可能不同于本次审查快照"
              @click.stop="goFile(f.project_id, f.file_id)"
            >
              编辑当前版本
            </el-button>
          </div>
          <EmptyState v-if="fileList.length === 0" description="暂无审查文件" compact />
        </div>
      </aside>

      <!-- 中：Monaco 代码 -->
      <main class="pane pane-code" :aria-busy="loadingCode">
        <header class="pane-head pane-head-code">
          <div class="code-file font-mono">
            <span class="file-ico">{{ fileGlyph(currentFileName) }}</span>
            <span>{{ currentFileName || '请选择文件' }}</span>
            <span v-if="currentLanguage" class="lang-chip font-mono">{{ currentLanguage }}</span>
          </div>
          <div class="code-tools">
            <el-button-group>
              <el-button size="small" :icon="ZoomIn" aria-label="放大代码字号" title="放大字号" @click="codeViewerRef?.zoomIn()" />
              <el-button size="small" :icon="ZoomOut" aria-label="缩小代码字号" title="缩小字号" @click="codeViewerRef?.zoomOut()" />
            </el-button-group>
          </div>
        </header>
        <div
          v-if="currentFileId !== null"
          class="code-provenance"
          :class="{ 'is-warning': previewSource === 'current', 'is-error': !!codeError }"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <strong>{{ codeProvenanceTitle }}</strong>
          <span v-if="loadingCode">{{ previewSource === 'snapshot' ? '正在读取并校验审查输入，校验完成前不展示内容。' : '正在读取当前文件内容。' }}</span>
          <span v-else-if="previewVerified">SHA-256 已校验；内容与任务冻结的摘要一致。编辑入口打开当前版本，不修改此快照。</span>
          <span v-else-if="previewSource === 'current'">当前文件，无法证明当时输入；此预览不能作为本次审查输入的证据。</span>
          <span v-else>未通过读取与完整性校验，不展示内容，也不以当前版本替代。</span>
        </div>
        <div class="pane-body code-body">
          <div v-if="loadingCode" class="loading-box"><el-skeleton :rows="14" animated /></div>
          <div v-else-if="codeError" class="code-error" role="alert">
            <p>{{ codeError }}</p>
            <el-button @click="retryFileCode">重新加载代码</el-button>
          </div>
          <EmptyState v-else-if="!codeContent && !currentIsBinary" description="请从左侧选择一个文件查看代码" />
          <CodeViewer
            v-else
            ref="codeViewerRef"
            :code="codeContent"
            :language="currentLanguage"
            :is-binary="currentIsBinary"
            :binary-meta="currentBinaryMeta"
            height="100%"
            :highlight-lines="highlightLines"
          />
        </div>
      </main>

      <!-- 右：问题列表 -->
      <aside class="pane pane-issues">
        <header class="pane-head">
          <span class="font-display">问题列表</span>
          <span class="font-mono pane-count">{{ issueTotal }}</span>
        </header>

        <!-- 严重度过滤 -->
        <div class="filter-chips">
          <button
            v-for="sev in severityChips"
            :key="sev.key"
            class="chip"
            :class="{ active: filter.severity === sev.value }"
            @click="toggleSeverity(sev.value)"
          >
            <span class="chip-dot" :style="{ background: sev.color }"></span>
            <span>{{ sev.label }}</span>
            <span class="chip-num font-mono">{{ sev.count }}</span>
          </button>
        </div>

        <!-- 维度过滤 -->
        <div class="filter-dims">
          <button
            v-for="d in dimChips"
            :key="d.key"
            class="dim-chip"
            :class="{ active: filter.issue_type === d.key }"
            :style="{ '--dim-color': d.color }"
            @click="toggleDim(d.key)"
          >
            <span class="dim-dot" :style="{ background: d.color }"></span>{{ d.label }}
          </button>
        </div>

        <div class="pane-body issue-body">
          <div v-if="issuesError" class="issues-error" role="alert">
            <p>问题列表更新失败：{{ issuesError }}。</p>
            <p v-if="issues.length">已有内容为上次成功获取的结果。</p>
            <el-button :loading="issuesLoading" :disabled="issuesLoading" @click="retryIssues">重新加载问题</el-button>
          </div>
          <div
            v-for="issue in issues"
            :key="issue.id"
            class="issue-row"
            role="button"
            tabindex="0"
            :class="{ active: selectedIssue?.id === issue.id }"
            @click="onIssueClick(issue)"
            @keydown.enter.prevent="onIssueClick(issue)"
            @keydown.space.prevent="onIssueClick(issue)"
          >
            <span class="sev" :class="`sev-${severityClass(issue.severity)}`">{{ severityDisplayLabel(issue.severity) }}</span>
            <div class="issue-meta">
              <div class="issue-title">{{ issue.title || issue.description.slice(0, 40) || '未命名问题' }}</div>
              <div class="issue-sub font-mono">
                {{ issue.file_name || '未知文件' }} · L{{ issue.line_number ?? '?' }}
                <span class="dim-dot" :style="{ background: dimColor(issue.issue_type) }"></span>
                {{ issueTypeLabel(issue.issue_type) }}
              </div>
            </div>
            <span class="issue-status font-mono" :class="`is-${issue.status}`">{{ issueStatusLabel(issue.status) }}</span>
          </div>
          <EmptyState v-if="!issuesError && issues.length === 0" :description="task?.status === 'success' ? '没有匹配的问题' : '尚无可展示的问题，不代表审查通过'" compact />
        </div>

        <footer v-if="issueTotal > issuePageSize" class="pane-foot">
          <el-pagination
            v-model:current-page="issuePage"
            :page-size="issuePageSize"
            :total="issueTotal"
            layout="prev, pager, next"
            small
            @current-change="onIssuePage"
          />
        </footer>
      </aside>
    </section>

    <IssueDetailDrawer
      v-model="drawerVisible"
      :issue="selectedIssue"
      @reviewed="onIssueReviewed"
    />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { goBack } from '@/utils/navigation'
import { ArrowLeft, Lock, MagicStick, ZoomIn, ZoomOut, RefreshRight } from '@element-plus/icons-vue'

import CodeViewer from '@/components/code/CodeViewer.vue'
import IssueDetailDrawer from '@/components/issue/IssueDetailDrawer.vue'
import AiPromptModal from '@/components/issue/AiPromptModal.vue'
import SecurityScanModal from '@/components/security/SecurityScanModal.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getReviewTaskDetail, getTaskIssues } from '@/api/review'
import { getDetail as getCodeFileDetail, getVersion as getCodeFileVersion } from '@/api/codeFile'
import type { TaskDetailOut, TaskFileOut, IssueOut } from '@/types/review'
import type { CodeFileMetaOut } from '@/types/project'
import { PRISM_SEVERITY_COLORS } from '@/components/chart/prismTheme'
import { SEVERITY_OPTIONS, severityClass, severityDisplayLabel } from '@/constants/severity'
import { DIM_META, normalizeDimKey, dimColor as resolveDimColor, dimLabel as resolveDimLabel } from '@/constants/dim'
import { reviewTypeLabel } from '@/constants/reviewType'

const route = useRoute()
const router = useRouter()
const taskId = computed(() => Number(route.params.id))

type TraceFileItem = Partial<TaskFileOut> & {
  file_id: number
  file_name: string
  project_id?: number
}

const pageLoading = ref(true)
const pageError = ref('')
const detailError = ref('')
const detailRequestId = ref('')
const detailNextAction = ref('')
const issuesError = ref('')
const codeError = ref('')
const refreshing = ref(false)
const issuesLoading = ref(false)
let disposed = false
let viewGeneration = 0
let issueSequence = 0
let codeSequence = 0
let issuesAccessRevoked = false
const snapshotFileIds = new Set<number>()
let detailRequest: { generation: number; promise: Promise<void> } | null = null
let issueRequest: { key: string; promise: Promise<void> } | null = null
const task = ref<TaskDetailOut | null>(null)
const isSandboxReport = computed(() => task.value?.review_type === 'sandbox_test')
const isTestScore = computed(() => ['sandbox_test', 'pentest'].includes(task.value?.review_type || ''))
function tallyCount(level: string, fallback: number | undefined): number | string {
  if (!isSandboxReport.value) return fallback ?? 0
  const summary = task.value?.report_issue_summary
  if (summary?.total == null || summary.total === summary.unclassified && summary.total > 0) return '—'
  return summary.severity_counts[level] ?? 0
}
const displayScore = computed(() => {
  const value = task.value?.score
  return task.value?.status === 'success' && typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null
})
const modelLabel = computed(() => task.value?.model_name?.trim() || '模型未知（接口未提供）')
const stageLabel = computed(() => {
  if (isSandboxReport.value) return statusLabels[task.value?.status ?? ''] || '状态未记录'
  const stage = task.value?.coverage?.stage
  const labels: Record<string, string> = {
    queued: '等待执行', analyzing: '分析中', complete: '执行完成',
    failed: '执行失败', cancelled: '已取消', deleted: '已删除',
  }
  return stage ? labels[stage] || `未知阶段（${stage}）` : '阶段未知（接口未提供）'
})
const taskFailure = computed(() => task.value?.coverage?.error || task.value?.error_message || (task.value?.status === 'failed' ? '审查失败，接口未提供具体原因。' : ''))

function coverageCount(value: unknown): string {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? String(value) : '未知'
}

function requestError(error: unknown, fallback: string): string {
  const failure = error as { message?: unknown } | null
  return typeof failure?.message === 'string' && failure.message.trim() ? failure.message : fallback
}

function isReadAccessFailure(error: unknown): boolean {
  const failure = error as { code?: unknown; response?: { status?: number } } | null
  const status = typeof failure?.code === 'number' ? Math.floor(failure.code / 100) : failure?.response?.status
  return status === 401 || status === 403 || status === 404
}

function recordDetailFailure(error: unknown): void {
  const failure = error as { code?: unknown; request_id?: unknown; next_action?: unknown; response?: { status?: number } } | null
  detailError.value = requestError(error, '无法连接审查服务')
  detailRequestId.value = typeof failure?.request_id === 'string' ? failure.request_id : ''
  detailNextAction.value = typeof failure?.next_action === 'string' ? failure.next_action : ''
  if (isReadAccessFailure(error)) {
    // 权限/资源已失效时不能继续显示缓存内容；也不能让迟到请求重新填充它。
    issueSequence++
    codeSequence++
    issuesAccessRevoked = true
    issueRequest = null
    task.value = null
    issues.value = []
    issueTotal.value = 0
    issuesLoading.value = false
    fileList.value = []
    snapshotFileIds.clear()
    currentFileId.value = null
    currentFileName.value = ''
    currentLanguage.value = 'text'
    codeContent.value = ''
    codeError.value = ''
    loadingCode.value = false
    currentIsBinary.value = false
    currentBinaryMeta.value = null
    previewSource.value = null
    previewVerified.value = false
    snapshotVersion.value = null
    selectedIssue.value = null
    drawerVisible.value = false
    aiPromptVisible.value = false
    securityScanVisible.value = false
  }
  if (!task.value) pageError.value = `加载审查任务详情失败：${detailError.value}`
}
const issues = ref<IssueOut[]>([])
const issueTotal = ref(0)
const issuePage = ref(1)
const issuePageSize = ref(50)
const filter = ref<{ severity: string; issue_type: string; status: string }>({
  severity: '',
  issue_type: '',
  status: '',
})

const currentFileId = ref<number | null>(null)
const currentFileName = ref('')
const currentLanguage = ref('text')
const codeContent = ref('')
/** v3: 当前文件是否为二进制文件 */
const currentIsBinary = ref(false)
/** v3: 当前二进制文件的元信息(仅 currentIsBinary 为 true 时使用) */
const currentBinaryMeta = ref<CodeFileMetaOut | null>(null)
const loadingCode = ref(false)
const previewSource = ref<'snapshot' | 'current' | null>(null)
const previewVerified = ref(false)
const snapshotVersion = ref<number | null>(null)
const codeProvenanceTitle = computed(() => {
  if (previewSource.value === 'current') return '当前内容，历史输入未知'
  const version = snapshotVersion.value === null ? '版本未知' : `v${snapshotVersion.value}`
  const state = previewVerified.value ? '快照' : loadingCode.value ? '快照待校验' : '快照不可用'
  return `审查输入 ${version} / ${state}`
})
const fileList = ref<TraceFileItem[]>([])

const drawerVisible = ref(false)
const selectedIssue = ref<IssueOut | null>(null)
const codeViewerRef = ref<InstanceType<typeof CodeViewer> | null>(null)
const aiPromptVisible = ref(false)
const securityScanVisible = ref(false)

function discardIssueSnapshot(): void {
  issuesAccessRevoked = true
  issues.value = []
  issueTotal.value = 0
  selectedIssue.value = null
  drawerVisible.value = false
  // 保留详情接口仍授权的文件；移除仅由已失效的问题列表补充的文件信息。
  fileList.value = (task.value?.files ?? []).map((file) => ({ ...file }))
  codeSequence++
  currentFileId.value = null
  currentFileName.value = ''
  currentLanguage.value = 'text'
  codeContent.value = ''
  codeError.value = ''
  loadingCode.value = false
  currentIsBinary.value = false
  currentBinaryMeta.value = null
  previewSource.value = null
  previewVerified.value = false
  snapshotVersion.value = null
  aiPromptVisible.value = false
  securityScanVisible.value = false
}

const statusLabels: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const dimMeta = DIM_META.map((d) => ({ key: d.key, label: d.name, color: d.color }))

const issueStatusMap: Record<string, string> = {
  unfixed: '未修复', fixed: '已修复', ignored: '已忽略', pending_review: '待审核',
}

const severityChips = computed(() => {
  const countMap = {
    严重: task.value?.severe_issues ?? 0,
    高: task.value?.high_issues ?? 0,
    中: task.value?.medium_issues ?? 0,
    低: task.value?.low_issues ?? 0,
  }
  return SEVERITY_OPTIONS.map((option) => ({
    ...option,
    color: PRISM_SEVERITY_COLORS[option.key],
    count: countMap[option.value],
  }))
})

const dimChips = computed(() => dimMeta)

const riskLevel = computed(() => {
  if (isTestScore.value) return '测试得分不代表安全风险评级'
  const score = displayScore.value
  if (score === null) return ''
  if (score >= 90) return '优秀 · 以审查范围为准'
  if (score >= 80) return '良好 · 关注潜在风险'
  if (score >= 70) return '一般 · 建议修复'
  if (score >= 60) return '及格 · 需要重构'
  return '风险 · 必须处理'
})

/**
 * 获取审查任务状态的中文文案
 * @param s - 任务状态枚举值
 * @returns 中文状态文案
 */
function statusLabel(s: string): string {
  return statusLabels[s] ?? s
}

/**
 * 获取问题类型的中文文案 (v2.0: 使用 normalizeDimKey 兼容多种枚举写法)
 * @param s - 问题类型枚举值
 * @returns 中文问题类型文案
 */
function issueTypeLabel(s: string): string {
  return resolveDimLabel(s)
}

/**
 * 获取问题类型对应的主题色 (v2.0: 通过归一化兜底)
 * @param s - 问题类型枚举值
 * @returns CSS 颜色值
 */
function dimColor(s: string): string {
  if (normalizeDimKey(s)) return resolveDimColor(s)
  return 'var(--gray-400)'
}

/**
 * 获取问题处理状态的中文文案
 * @param s - 问题处理状态枚举值
 * @returns 中文状态文案
 */
function issueStatusLabel(s: string): string {
  return issueStatusMap[s] ?? s
}

function fileGlyph(name?: string): string {
  if (!name) return '·'
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  return ext.slice(0, 3).toUpperCase() || '·'
}

function fileIssueCount(fileId: number): number {
  return issues.value.filter((i) => i.file_id === fileId).length
}

function formatDuration(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const seconds = Math.round(ms / 1000)
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`
}

function scoreGradient(score: number): string {
  if (score >= 85) return 'linear-gradient(135deg, #4FB87A, #2BBFB9)'
  if (score >= 70) return 'linear-gradient(135deg, #D9A857, #4FB87A)'
  if (score >= 60) return 'linear-gradient(135deg, #E27C4A, #D9A857)'
  return 'linear-gradient(135deg, #DC4961, #E27C4A)'
}

function scoreFlatColor(score: number): string {
  if (score >= 85) return 'var(--status-fixed)'
  if (score >= 70) return 'var(--sev-medium)'
  if (score >= 60) return 'var(--sev-high)'
  return 'var(--sev-severe)'
}

const highlightLines = computed(() => {
  if (!selectedIssue.value || selectedIssue.value.file_id !== currentFileId.value) return []
  const start = selectedIssue.value.line_number ?? 0
  const end = selectedIssue.value.end_line ?? start
  if (!start) return []
  const lines: number[] = []
  for (let i = start; i <= end; i++) lines.push(i)
  return lines
})

function loadTaskDetail(): Promise<void> {
  if (detailRequest?.generation === viewGeneration) return detailRequest.promise
  const generation = viewGeneration
  const promise = getReviewTaskDetail(taskId.value).then((data) => {
    if (disposed || generation !== viewGeneration) return
    if (!data) throw new Error('接口未返回审查任务详情')
    task.value = data
    detailError.value = ''
    detailRequestId.value = ''
    detailNextAction.value = ''
    pageError.value = ''
    fileList.value = (data.files ?? []).map((item) => ({ ...item }))
    for (const file of data.files ?? []) {
      if (file.snapshot_verified === true || file.content_sha256 != null) snapshotFileIds.add(file.file_id)
    }
  }).catch((error: unknown) => {
    if (disposed || generation !== viewGeneration) return
    recordDetailFailure(error)
  }).finally(() => {
    if (detailRequest?.promise === promise) detailRequest = null
  })
  detailRequest = { generation, promise }
  return promise
}

function doLoadIssues(): Promise<void> {
  const params: Record<string, unknown> = {
    page: issuePage.value,
    page_size: issuePageSize.value,
    ...Object.fromEntries(Object.entries(filter.value).filter(([, value]) => value)),
  }
  if (!params.status) params.status = 'all'
  const generation = viewGeneration
  const key = JSON.stringify([generation, taskId.value, params])
  if (issueRequest?.key === key) return issueRequest.promise
  const sequence = ++issueSequence
  issuesLoading.value = true
  const promise = getTaskIssues(taskId.value, params).then((data) => {
    if (disposed || generation !== viewGeneration || sequence !== issueSequence) return
    issues.value = data.items
    issueTotal.value = data.total
    issuesError.value = ''
    issuesAccessRevoked = false

    const fileSet = new Map<number, TraceFileItem>()
    fileList.value.forEach((item) => fileSet.set(item.file_id, item))
    data.items.forEach((item) => {
      if (item.file_id && item.file_name && !fileSet.has(item.file_id)) {
        fileSet.set(item.file_id, {
          file_id: item.file_id,
          file_name: item.file_name,
          project_id: task.value?.project_id,
        })
      }
    })
    fileList.value = Array.from(fileSet.values())
  }).catch((error: unknown) => {
    if (disposed || generation !== viewGeneration || sequence !== issueSequence) return
    if (isReadAccessFailure(error)) discardIssueSnapshot()
    issuesError.value = requestError(error, '无法获取问题列表')
  }).finally(() => {
    if (!disposed && sequence === issueSequence) issuesLoading.value = false
    if (issueRequest?.promise === promise) issueRequest = null
  })
  issueRequest = { key, promise }
  return promise
}

async function retryIssues(): Promise<void> {
  await doLoadIssues()
  schedulePollIfRunning()
}

async function loadAllData() {
  if (refreshing.value || disposed) return
  const generation = viewGeneration
  refreshing.value = true
  stopPolling()
  // 手动重试保留失败原因与返回入口，避免再次被全页 loading 锁住。
  pageLoading.value = !task.value && !pageError.value
  try {
    await loadTaskDetail()
    if (!disposed && generation === viewGeneration && task.value && !detailError.value) await doLoadIssues()
  } finally {
    if (!disposed && generation === viewGeneration) {
      pageLoading.value = false
      refreshing.value = false
      schedulePollIfRunning()
    }
  }
}

// ── 运行中任务轮询: 后端异步执行审查,前端定时刷新进度直至终态 ──
const POLL_INTERVAL = 3000
let pollTimer: ReturnType<typeof setTimeout> | null = null

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePollIfRunning() {
  stopPolling()
  if (disposed || refreshing.value || detailError.value || issuesError.value || !['pending', 'running'].includes(task.value?.status || '')) return
  const generation = viewGeneration
  pollTimer = setTimeout(async () => {
    pollTimer = null
    await loadTaskDetail()
    if (disposed || generation !== viewGeneration) return
    if (!detailError.value) await doLoadIssues()
    if (disposed || generation !== viewGeneration) return
    schedulePollIfRunning()
  }, POLL_INTERVAL)
}

async function loadFileCode(fileId: number) {
  const sequence = ++codeSequence
  const generation = viewGeneration
  const taskFile = task.value?.files?.find((file) => file.file_id === fileId)
  const hasSnapshot = snapshotFileIds.has(fileId) || taskFile?.snapshot_verified === true || taskFile?.content_sha256 != null
  previewSource.value = hasSnapshot ? 'snapshot' : 'current'
  previewVerified.value = false
  snapshotVersion.value = typeof taskFile?.version_no === 'number' && Number.isInteger(taskFile.version_no) && taskFile.version_no > 0 ? taskFile.version_no : null
  currentFileName.value = taskFile?.file_name || fileList.value.find((file) => file.file_id === fileId)?.file_name || ''
  currentLanguage.value = taskFile?.language || 'text'
  codeContent.value = ''
  currentIsBinary.value = false
  currentBinaryMeta.value = null
  loadingCode.value = true
  codeError.value = ''
  try {
    if (hasSnapshot) {
      if (!taskFile || taskFile.snapshot_verified !== true) {
        throw new Error('快照完整性校验失败：服务端未通过校验，重试将重新获取任务记录')
      }
      const versionNo = taskFile.version_no
      const expectedHash = taskFile.content_sha256
      if (typeof versionNo !== 'number' || !Number.isInteger(versionNo) || versionNo < 1 || typeof expectedHash !== 'string' || !/^[a-f0-9]{64}$/i.test(expectedHash)) {
        throw new Error('快照元数据缺失或无效，无法确认历史版本与内容摘要')
      }
      if (typeof globalThis.crypto?.subtle?.digest !== 'function' || typeof TextEncoder !== 'function') {
        throw new Error('浏览器不支持快照完整性校验，请使用支持 WebCrypto 的安全浏览器环境')
      }
      const version = await getCodeFileVersion(fileId, versionNo)
      if (disposed || generation !== viewGeneration || sequence !== codeSequence) return
      if (version?.file_id !== fileId || version.version_no !== versionNo || typeof version.content !== 'string') {
        throw new Error('历史版本响应与任务文件或版本不符，或缺少原始内容')
      }
      const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(version.content))
      if (disposed || generation !== viewGeneration || sequence !== codeSequence) return
      const actualHash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
      if (actualHash !== expectedHash.toLowerCase()) {
        throw new Error('SHA-256 不匹配，历史内容与审查输入不一致')
      }
      codeContent.value = version.content
      previewVerified.value = true
      return
    }
    const file = await getCodeFileDetail(fileId)
    if (disposed || generation !== viewGeneration || sequence !== codeSequence) return
    // v3: 二进制文件 content 由后端置空,不展示 Monaco,改为 CodeViewer 内部的二进制提示卡片
    currentIsBinary.value = file.is_binary === 1
    if (currentIsBinary.value) {
      codeContent.value = ''
      currentBinaryMeta.value = {
        id: file.id,
        file_name: file.file_name,
        file_path: file.file_path,
        language: file.language,
        size_bytes: file.size_bytes,
        raw_size: file.raw_size,
        line_count: file.line_count,
        version_no: file.version_no,
        is_binary: file.is_binary,
        mime_type: file.mime_type,
        md5_hash: file.md5_hash,
        sha256_hash: file.sha256_hash,
        create_time: file.create_time,
        update_time: file.update_time,
      }
    } else {
      codeContent.value = file.content
      currentBinaryMeta.value = null
    }
    currentLanguage.value = file.language
    currentFileName.value = file.file_name
  } catch (error: unknown) {
    if (disposed || generation !== viewGeneration || sequence !== codeSequence) return
    codeError.value = hasSnapshot
      ? `无法读取原始审查输入：${requestError(error, '读取或校验失败')}。不会改用当前文件。`
      : requestError(error, '无法获取代码内容，请重试')
  } finally {
    if (!disposed && generation === viewGeneration && sequence === codeSequence) loadingCode.value = false
  }
}

async function retryFileCode() {
  const fileId = currentFileId.value
  if (fileId === null || loadingCode.value) return
  const taskFile = task.value?.files?.find((file) => file.file_id === fileId)
  if ((snapshotFileIds.has(fileId) || taskFile?.content_sha256 != null) && taskFile?.snapshot_verified !== true) {
    const generation = viewGeneration
    const sequence = codeSequence
    loadingCode.value = true
    await loadTaskDetail()
    if (disposed || generation !== viewGeneration || sequence !== codeSequence) return
    loadingCode.value = false
    if (detailError.value) {
      codeError.value = `无法重新确认原始审查输入：${detailError.value}。不会改用当前文件。`
      return
    }
  }
  await loadFileCode(fileId)
}

function onFilePick(fileId: number) {
  if (currentFileId.value === fileId) return
  currentFileId.value = fileId
  loadFileCode(fileId)
}

function onIssueClick(issue: IssueOut) {
  if (issuesAccessRevoked || disposed) return
  selectedIssue.value = issue
  if (issue.file_id && currentFileId.value !== issue.file_id) {
    currentFileId.value = issue.file_id
    loadFileCode(issue.file_id).then(() => {
      if (issue.line_number) {
        setTimeout(() => codeViewerRef.value?.revealLine(issue.line_number!), 300)
      }
    })
  } else if (issue.line_number) {
    codeViewerRef.value?.revealLine(issue.line_number)
  }
  drawerVisible.value = true
}

function showPendingReviews(): void {
  filter.value.status = 'pending_review'
  issuePage.value = 1
  doLoadIssues()
}

async function onIssueReviewed(updated: IssueOut): Promise<void> {
  if (issuesAccessRevoked || disposed) return
  selectedIssue.value = updated
  const index = issues.value.findIndex((item) => item.id === updated.id)
  if (index >= 0) issues.value.splice(index, 1, updated)
  await Promise.all([loadTaskDetail(), doLoadIssues()])
}

/**
 * 切换严重度筛选条件，并使用后端认可的中文枚举值请求数据
 * @param value - 后端严重度枚举值
 * @returns void
 */
function toggleSeverity(value: string): void {
  filter.value.severity = filter.value.severity === value ? '' : value
  issuePage.value = 1
  doLoadIssues()
}

function toggleDim(key: string) {
  filter.value.issue_type = filter.value.issue_type === key ? '' : key
  issuePage.value = 1
  doLoadIssues()
}

function onIssuePage(p: number) {
  issuePage.value = p
  doLoadIssues()
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
 * 跳转到报告详情页。
 * @param id - 审查任务/报告 ID
 * @returns void
 */
function goReport(id: number): void {
  router.push(`/reports/${id}`)
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

watch(taskId, () => {
  viewGeneration++
  issueSequence++
  codeSequence++
  snapshotFileIds.clear()
  stopPolling()
  task.value = null
  fileList.value = []
  issues.value = []
  issueTotal.value = 0
  issuePage.value = 1
  currentFileId.value = null
  currentFileName.value = ''
  currentLanguage.value = 'text'
  codeContent.value = ''
  previewSource.value = null
  previewVerified.value = false
  snapshotVersion.value = null
  currentIsBinary.value = false
  currentBinaryMeta.value = null
  loadingCode.value = false
  codeError.value = ''
  detailError.value = ''
  detailRequestId.value = ''
  detailNextAction.value = ''
  pageError.value = ''
  issuesError.value = ''
  issuesAccessRevoked = false
  selectedIssue.value = null
  drawerVisible.value = false
  aiPromptVisible.value = false
  securityScanVisible.value = false
  refreshing.value = false
  void loadAllData()
})

onMounted(() => {
  loadAllData()
})

onUnmounted(() => {
  disposed = true
  viewGeneration++
  stopPolling()
})
</script>

<style scoped lang="scss">
.review-detail-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
}

.page-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 320px;
}

.recovery-hint,
.request-id { margin: 6px 0; color: var(--gray-600); overflow-wrap: anywhere; }
.recovery-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.recovery-actions :deep(.el-button + .el-button) { margin-left: 0; }

.execution-panel {
  padding: 16px 20px;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  background: #fff;
}

.execution-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  color: var(--gray-600);
  font-size: 12px;

  strong { color: var(--gray-900); font-size: 15px; }
}

.coverage-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 14px 0 0;

  dt { color: var(--gray-600); font-size: 12px; margin-bottom: 6px; }
  dd { margin: 0; color: var(--gray-900); font-size: 14px; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
}

.coverage-note { margin: 12px 0 0; color: var(--gray-600); font-size: 12px; line-height: 1.6; }
.score-unavailable { font-size: 12px; color: var(--gray-600); }
.stale-note { color: #a62b43; }
.execution-error,
.connection-error,
.issues-error,
.code-error {
  padding: 12px 14px;
  border: 1px solid #f0c4cc;
  border-radius: 8px;
  background: #fff4f5;
  color: #a62b43;
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;

  p { margin: 6px 0 10px; }
}
.execution-error { margin-top: 12px; }

.file-row:focus-visible,
.issue-row:focus-visible,
.chip:focus-visible,
.dim-chip:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }

@media (max-width: 680px) {
  .coverage-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  .pill-dot,
  .file-row,
  .issue-row,
  .chip,
  .dim-chip,
  :deep(.el-button *) { animation: none !important; transition: none !important; }
}

/* ============ AI 进度光带 ============ */
.ai-ribbon {
  margin-bottom: 0;
}

/* ============ 顶部摘要条 ============ */
.task-head {
  display: grid;
  grid-template-columns: auto 1fr auto auto auto;
  gap: 18px;
  align-items: center;
  padding: 16px 20px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  box-shadow: var(--shadow-1);
}

.trust-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  min-height: 38px;
  padding: 6px 16px;
  border-left: 3px solid var(--brand-500);
  background: #fff;
  color: var(--gray-600);
  font-size: 12px;

  .trust-title {
    color: var(--gray-900);
    font-weight: 600;
  }

  .attention {
    color: var(--el-color-warning-dark-2);
    font-weight: 600;
  }
}

.back-btn {
  font-size: 13px;
}

.head-title {
  min-width: 0;

  h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--gray-900);
    letter-spacing: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .head-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
    font-size: 11.5px;
    color: var(--gray-500);

    .dot { opacity: 0.5; }
  }

  .trace-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
    min-width: 0;
    color: var(--gray-500);
    font-size: 12px;

    :deep(.el-button) {
      max-width: 100%;
      height: auto;
      margin-left: 0;
      white-space: normal;
      text-align: left;
    }

    :deep(.el-button > span),
    :deep(.el-tag__content) {
      min-width: 0;
      overflow-wrap: anywhere;
      white-space: normal;
    }

    :deep(.el-tag) {
      max-width: 100%;
      height: auto;
      min-height: 24px;
      padding-block: 3px;
      white-space: normal;
    }
  }
}

.head-score {
  display: flex;
  align-items: center;
  gap: 12px;
}

.score-orb {
  position: relative;
  width: 58px;
  height: 58px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  flex-direction: column;
  flex-shrink: 0;
  gap: 2px;
  justify-content: center;
  color: #fff;
  font-weight: 600;
  box-shadow: 0 8px 20px -8px rgba(91, 88, 232, 0.4);
}

.score-val {
  font-size: 22px;
  line-height: 1;
}

.score-out {
  font-size: 10px;
  opacity: 0.85;
}

.score-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.score-label {
  font-size: 11px;
  color: var(--gray-500);
  font-family: var(--font-mono);
}

.score-status {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font-display);
}

.head-tally {
  display: inline-flex;
  align-items: center;
  gap: 14px;
  padding: 10px 14px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 10px;
}

.tally-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.t-val {
  font-size: 18px;
  font-weight: 600;
  line-height: 1;
}

.t-label {
  font-size: 10.5px;
  color: var(--gray-500);
  font-family: var(--font-mono);
}

.tally-divider {
  width: 1px;
  height: 26px;
  background: var(--gray-200);
}

.head-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;

  .pill-dot { width: 6px; height: 6px; border-radius: 50%; }

  &.s-running {
    color: var(--brand-600); background: var(--brand-50);
    .pill-dot { background: var(--brand-500); box-shadow: 0 0 8px var(--brand-500); animation: prismPulse 1.6s ease-in-out infinite; }
  }
  &.s-success {
    color: var(--status-fixed); background: rgba(79, 184, 122, 0.12);
    .pill-dot { background: var(--status-fixed); }
  }
  &.s-failed {
    color: var(--sev-severe); background: var(--sev-severe-bg);
    .pill-dot { background: var(--sev-severe); }
  }
  &.s-pending, &.s-cancelled {
    color: var(--gray-500); background: var(--gray-100);
    .pill-dot { background: var(--gray-400); }
  }
}

/* ============ 三栏 ============ */
.workbench {
  display: grid;
  grid-template-columns: 240px 1fr 360px;
  gap: 14px;
  flex: 1;
  min-height: 0;
}

@media (max-width: 1280px) {
  .workbench { grid-template-columns: 200px 1fr 320px; }
}

@media (max-width: 900px) {
  .review-detail-page {
    height: auto;
  }

  .task-head {
    grid-template-columns: minmax(0, 1fr);
    align-items: stretch;
  }

  .head-score,
  .head-tally,
  .head-actions {
    justify-content: flex-start;
  }

  .head-tally {
    flex-wrap: wrap;
  }

  .workbench {
    grid-template-columns: minmax(0, 1fr);
    min-height: auto;
  }

  .pane {
    min-height: 260px;
  }

  .pane-code {
    min-height: 420px;
  }
}

.pane {
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: var(--shadow-1);
  min-height: 0;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--gray-100);
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
}

.pane-count {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--gray-100);
  color: var(--gray-600);
  border-radius: 999px;
  font-weight: 500;
}

.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  min-height: 0;
}

.pane-head-code {
  padding: 10px 14px;
}

.code-file {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--gray-800);

  .file-ico {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--gray-100);
    color: var(--gray-600);
    font-size: 9.5px;
  }

  .lang-chip {
    height: 20px;
    padding: 0 8px;
    border-radius: 4px;
    background: var(--brand-50);
    color: var(--brand-600);
    font-size: 10px;
    display: inline-flex;
    align-items: center;
  }
}

.code-provenance {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 4px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--brand-50);
  color: var(--gray-700);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;

  &.is-warning { background: var(--el-color-warning-light-9); }
  &.is-error { background: var(--el-color-danger-light-9); }
}

.code-body {
  padding: 0;
  position: relative;
}

.code-body :deep(.monaco-editor-wrapper) {
  border: none;
  border-radius: 0;
  height: 100%;
}

.loading-box {
  padding: 24px;
  height: 100%;
}

/* ============ 文件列表 ============ */
.file-row {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { background: var(--gray-50); color: var(--gray-900); }

  &.active {
    background: var(--brand-50);
    color: var(--brand-700);
    font-weight: 500;
  }

  .file-ico {
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--gray-100);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 9.5px;
    color: var(--gray-600);
  }

  .file-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-issues {
    font-size: 11px;
    font-family: var(--font-mono);
    font-weight: 600;
  }

  .file-open {
    opacity: 0;
    transition: opacity 0.12s ease;
  }

  &:hover .file-open,
  &.active .file-open {
    opacity: 1;
  }
}

/* ============ 问题过滤 chip ============ */
.filter-chips {
  display: flex;
  gap: 6px;
  padding: 10px 12px 6px;
  flex-wrap: wrap;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 7px;
  font-size: 11.5px;
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { border-color: var(--brand-200); }
  &.active {
    background: var(--brand-50);
    border-color: var(--brand-200);
    color: var(--brand-700);
  }

  .chip-dot { width: 6px; height: 6px; border-radius: 50%; }
  .chip-num { color: var(--gray-500); font-size: 10.5px; }
  &.active .chip-num { color: var(--brand-600); }
}

.filter-dims {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 0 12px 10px;
  border-bottom: 1px solid var(--gray-100);
}

.dim-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 8px;
  background: transparent;
  border: 1px solid var(--gray-100);
  border-radius: 999px;
  font-size: 10.5px;
  color: var(--gray-600);
  cursor: pointer;
  transition: all 0.12s ease;

  .dim-dot { width: 6px; height: 6px; border-radius: 50%; }

  &:hover { border-color: var(--dim-color); color: var(--dim-color); }
  &.active {
    background: color-mix(in srgb, var(--dim-color) 12%, transparent);
    border-color: var(--dim-color);
    color: var(--dim-color);
    font-weight: 500;
  }
}

/* ============ 问题列表 ============ */
.issue-body {
  padding: 4px 8px;
}

.issue-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: flex-start;
  padding: 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { background: var(--gray-50); }

  &.active {
    background: var(--brand-50);
    box-shadow: inset 3px 0 0 var(--brand-500);
  }
}

.issue-meta {
  min-width: 0;
}

.issue-title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--gray-900);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.issue-sub {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--gray-500);

  .dim-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-left: 4px;
  }
}

.issue-status {
  font-size: 10.5px;
  padding: 2px 7px;
  border-radius: 4px;
  font-weight: 500;
  white-space: nowrap;

  &.is-unfixed       { color: var(--status-unfix); background: var(--sev-high-bg); }
  &.is-fixed         { color: var(--status-fixed); background: rgba(79, 184, 122, 0.12); }
  &.is-ignored       { color: var(--gray-500);    background: var(--gray-100); }
  &.is-pending_review{ color: var(--brand-600);   background: var(--brand-50); }
}

.pane-foot {
  border-top: 1px solid var(--gray-100);
  padding: 8px 12px;
  display: flex;
  justify-content: center;
}
</style>
