import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  permissions: new Set<string>(),
  routerPush: vi.fn(),
}))

const reviewApi = vi.hoisted(() => ({
  getReviewTasks: vi.fn(),
  deleteReviewTask: vi.fn(),
  cancelReviewTask: vi.fn(),
}))
const projectApi = vi.hoisted(() => ({ getProjects: vi.fn() }))
const issueApi = vi.hoisted(() => ({
  list: vi.fn(),
  updateStatus: vi.fn(),
  batchUpdateStatus: vi.fn(),
}))
const codeApi = vi.hoisted(() => ({
  getDetail: vi.fn(),
  update: vi.fn(),
  downloadBinary: vi.fn(),
}))
const reportApi = vi.hoisted(() => ({
  getReports: vi.fn(),
  deleteReport: vi.fn(),
  exportReport: vi.fn(),
}))
const securityApi = vi.hoisted(() => ({
  getSecurityChecklist: vi.fn(),
  getSecurityDashboard: vi.fn(),
}))
const dashboardApi = vi.hoisted(() => ({
  getSummary: vi.fn(),
  getRiskDistribution: vi.fn(),
  getIssueTypeStatistics: vi.fn(),
  getScoreTrend: vi.fn(),
  getReviewFrequency: vi.fn(),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    hasPermission: (code: string) => state.permissions.has(code),
    isAdmin: () => false,
  }),
}))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: state.routerPush }),
  useRoute: () => ({ params: { fileId: '7', projectId: '3' } }),
}))
vi.mock('@/api/review', () => reviewApi)
vi.mock('@/api/project', () => projectApi)
vi.mock('@/api/issue', () => issueApi)
vi.mock('@/api/codeFile', () => codeApi)
vi.mock('@/api/report', () => reportApi)
vi.mock('@/api/security', () => securityApi)
vi.mock('@/api/dashboard', () => dashboardApi)
vi.mock('@/components/editor/MonacoEditor.vue', () => ({
  default: { name: 'MonacoEditor', template: '<div class="monaco-editor-stub" />' },
}))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: vi.fn().mockResolvedValue(true) }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn().mockResolvedValue(true) },
}))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

import CodeEditor from '@/views/code/CodeEditor.vue'
import Dashboard from '@/views/dashboard/Dashboard.vue'
import IssueHub from '@/views/issue/IssueHub.vue'
import ReportList from '@/views/report/ReportList.vue'
import ReviewTaskList from '@/views/review/ReviewTaskList.vue'
import SecurityCenter from '@/views/security/SecurityCenter.vue'

const GenericStub = { template: '<div><slot /><slot name="empty" /><slot name="dropdown" /><slot name="footer" /></div>' }
const ButtonStub = { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /></button>' }

function mountView(component: object) {
  return shallowMount(component, {
    global: {
      directives: { loading: {} },
      stubs: {
        'el-button': ButtonStub,
        'el-card': GenericStub,
        'el-table': GenericStub,
        'el-table-column': { template: '<div />' },
        'el-select': GenericStub,
        'el-option': GenericStub,
        'el-input': GenericStub,
        'el-date-picker': GenericStub,
        'el-pagination': GenericStub,
        'el-dropdown': GenericStub,
        'el-dropdown-menu': GenericStub,
        'el-dropdown-item': GenericStub,
        'el-icon': GenericStub,
        'el-page-header': GenericStub,
        'el-dialog': GenericStub,
        'el-tooltip': GenericStub,
        'el-alert': GenericStub,
        'el-tag': GenericStub,
        'el-descriptions': GenericStub,
        'el-descriptions-item': GenericStub,
        EmptyState: GenericStub,
        MonacoEditor: GenericStub,
        SecurityScanModal: GenericStub,
        SecurityPostureCard: GenericStub,
        BaseChart: GenericStub,
        FluidProgress: GenericStub,
        PrismLoading: GenericStub,
        RouterLink: GenericStub,
      },
    },
  })
}

beforeEach(() => {
  state.permissions.clear()
  state.routerPush.mockReset()
  window.localStorage.setItem('prism_changelog_seen', 'v3.4')

  reviewApi.getReviewTasks.mockResolvedValue({ items: [], total: 0 })
  projectApi.getProjects.mockResolvedValue({ items: [], total: 0 })
  issueApi.list.mockResolvedValue({ items: [], total: 0 })
  codeApi.getDetail.mockResolvedValue({
    id: 7,
    file_name: 'read-only.ts',
    file_path: 'src/read-only.ts',
    language: 'typescript',
    content: 'const value = 1',
    is_binary: 0,
    version_no: 1,
    size_bytes: 15,
    update_time: '2026-08-28T00:00:00Z',
  })
  reportApi.getReports.mockResolvedValue({ items: [], total: 0 })
  securityApi.getSecurityChecklist.mockResolvedValue({ secret_patterns: [], static_rules: [] })
  securityApi.getSecurityDashboard.mockResolvedValue({
    project_count: 0,
    scanned_project_count: 0,
    avg_risk_score: null,
    severe_issues_total: 0,
    high_issues_total: 0,
    medium_issues_total: 0,
    low_issues_total: 0,
    owasp_hotspots: [],
    top_risky_projects: [],
    trend: [],
  })
  dashboardApi.getSummary.mockResolvedValue({
    project_count: 0,
    file_count: 0,
    review_count: 0,
    total_issues: 0,
    severe_issues: 0,
    avg_score: 0,
    recent_tasks: [],
  })
  dashboardApi.getRiskDistribution.mockResolvedValue([])
  dashboardApi.getIssueTypeStatistics.mockResolvedValue([])
  dashboardApi.getScoreTrend.mockResolvedValue([])
  dashboardApi.getReviewFrequency.mockResolvedValue([])
})

describe('细粒度权限的页内操作可见性', () => {
  it('只读审查账号看不到启动、停止或删除审查入口', async () => {
    const wrapper = mountView(ReviewTaskList)
    await flushPromises()

    expect(wrapper.text()).not.toContain('启动审查')
    expect((wrapper.vm as any).canStartReview).toBe(false)
    expect((wrapper.vm as any).canCancelReview).toBe(false)
    await (wrapper.vm as any).handleCancel({ id: 9, status: 'running', task_name: '只读任务' })
    await (wrapper.vm as any).handleDelete({ id: 9, status: 'success', task_name: '只读任务' })
    expect(reviewApi.cancelReviewTask).not.toHaveBeenCalled()
    expect(reviewApi.deleteReviewTask).not.toHaveBeenCalled()
  })

  it('只读问题账号看不到状态修改和批量处理', async () => {
    const wrapper = mountView(IssueHub)
    await flushPromises()

    expect(wrapper.text()).not.toContain('批量标记已修复')
    expect((wrapper.vm as any).canHandleIssues).toBe(false)
    expect((wrapper.vm as any).canBatchIssues).toBe(false)
    await (wrapper.vm as any).onSetStatus({ id: 3, status: 'unfixed' }, 'fixed')
    expect(issueApi.updateStatus).not.toHaveBeenCalled()
  })

  it('只有 file:view 时编辑器为只读且不显示保存', async () => {
    state.permissions.add('file:view')
    const wrapper = mountView(CodeEditor)
    await flushPromises()

    expect(wrapper.text()).not.toContain('保存 (Ctrl+S)')
    expect((wrapper.vm as any).canEdit).toBe(false)
    await (wrapper.vm as any).handleSave()
    expect(codeApi.update).not.toHaveBeenCalled()
  })

  it('只有 security:view 时不显示扫描操作', async () => {
    state.permissions.add('security:view')
    const wrapper = mountView(SecurityCenter)
    await flushPromises()

    expect(wrapper.text()).not.toContain('全量扫描')
    expect((wrapper.vm as any).canScan).toBe(false)
    ;(wrapper.vm as any).openAllProjectScan()
    expect((wrapper.vm as any).securityScanVisible).toBe(false)
  })

  it('报告导出格式按各自权限点二次拦截', async () => {
    const wrapper = mountView(ReportList)
    await flushPromises()

    expect((wrapper.vm as any).canExport('pdf')).toBe(false)
    await (wrapper.vm as any).handleExport({ task_id: 8, task_name: '只读报告' }, 'pdf')
    expect(reportApi.exportReport).not.toHaveBeenCalled()
  })

  it('仪表盘不向缺少 review:start 的账号显示新建审查', async () => {
    const wrapper = mountView(Dashboard)
    await flushPromises()

    expect(wrapper.text()).not.toContain('新建审查')
    expect(wrapper.text()).not.toContain('导出周报')
    expect((wrapper.vm as any).canStartReview).toBe(false)
    expect((wrapper.vm as any).canExportWeeklyReport).toBe(false)
  })
})
