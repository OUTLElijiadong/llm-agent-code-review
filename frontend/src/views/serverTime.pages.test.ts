/**
 * 同一个审查任务 #231 在各页面的时间必须一致（线上事故：任务列表/报告详情 16:20，报告列表/项目详情 08:20）。
 *
 * 库内时间 08:20（UTC），北京时间应显示 2026-10-22 16:20。本文件把同一条任务时间分别喂给
 * 审查任务列表、报告列表、报告详情、项目详情“最近任务”，并覆盖接口的几种历史输出形态
 * （无 Z、Z、+00:00、空格分隔）。package.json 的 test:time-zones 在 UTC / America/Los_Angeles /
 * Asia/Shanghai 三种进程时区下各跑一遍，结果必须一致。
 */
import { flushPromises, mount, shallowMount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useUserStore } from '@/stores/user'
import { deferred as _deferred, pageOf, scanMountOptions } from './review/scanRegressionTestUtils'

void _deferred

const api = vi.hoisted(() => ({
  tasks: vi.fn(), projects: vi.fn(), reports: vi.fn(), reportDetail: vi.fn(), projectDetail: vi.fn(),
  permissions: new Set<string>(),
}))
const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }))
const route = vi.hoisted(() => ({ params: { id: '231' }, query: {} as Record<string, string>, path: '/reports/231' }))

vi.mock('@/api/review', () => ({
  getReviewTasks: api.tasks, deleteReviewTask: vi.fn(), cancelReviewTask: vi.fn(), getTaskIssues: vi.fn(),
}))
vi.mock('@/api/report', () => ({
  getReports: api.reports, getReportDetail: api.reportDetail, exportReport: vi.fn(), deleteReport: vi.fn(),
  generateReport: vi.fn(), previewReport: vi.fn(),
}))
vi.mock('@/api/project', () => ({
  getProjects: api.projects, getProjectDetail: api.projectDetail, createProject: vi.fn(), updateProject: vi.fn(),
  deleteProject: vi.fn(), analyzeFolder: vi.fn(), getAuditSourceArchiveResult: vi.fn(), deleteSourceRevision: vi.fn(),
  downloadProjectSource: vi.fn(), uploadAuditSourceArchive: vi.fn(), queueRemoteProjectImport: vi.fn(),
  getRemoteProjectImport: vi.fn(), cancelRemoteProjectImport: vi.fn(),
}))
vi.mock('@/api/codeFile', () => ({ list: vi.fn(), downloadBinary: vi.fn(), upload: vi.fn(), uploadFolder: vi.fn() }))
vi.mock('@/api/projectMember', () => ({
  listProjectMembers: vi.fn().mockResolvedValue([]), addProjectMember: vi.fn(),
  updateProjectMemberRole: vi.fn(), removeProjectMember: vi.fn(),
}))
vi.mock('@/api/agent', () => ({
  listRuntimeAgents: vi.fn().mockResolvedValue([]), getSituation: vi.fn().mockResolvedValue(null),
  listTypeMappings: vi.fn().mockResolvedValue([]), listAgentSkills: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/utils/agentEventStream', () => ({ subscribeAgentEvents: () => ({ close: vi.fn() }) }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: vi.fn().mockResolvedValue(false) }))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
vi.mock('vue-router', async (original) => ({
  ...(await original<typeof import('vue-router')>()),
  useRouter: () => router,
  useRoute: () => route,
}))

import ProjectDetail from './project/ProjectDetail.vue'
import ReportDetail from './report/ReportDetail.vue'
import ReportList from './report/ReportList.vue'
import ReviewTaskList from './review/ReviewTaskList.vue'

/** 接口历史上出现过的几种输出形态，库内值都是 08:20 UTC。 */
const WIRE_SHAPES = [
  '2026-10-22T08:20:00',
  '2026-10-22T08:20:00Z',
  '2026-10-22T08:20:00+00:00',
  '2026-10-22 08:20:00',
] as const
const EXPECTED_MINUTE = '2026-10-22 16:20'

let wrapper: VueWrapper | undefined
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  document.body.innerHTML = ''
})

beforeEach(() => {
  vi.clearAllMocks()
  api.permissions.clear()
  for (const code of [
    'report:view', 'issue:view', 'review:view', 'project:view', 'file:view', 'agent:view',
    'report:export:json', 'report:export:html', 'report:export:pdf', 'report:export:word',
  ]) api.permissions.add(code)
  api.projects.mockResolvedValue(pageOf([]))
  // 页面直接使用真实的用户 store；先装好活动 Pinia 并授予查看权限。
  setActivePinia(createPinia())
  const store = useUserStore()
  store.profile = { id: 1, username: 'fixture', role: 'admin', status: 1 } as never
  store.permissions = new Set(api.permissions)
})

const ReportDetailStubs = {
  'el-button': { template: '<button><slot /></button>' },
  'el-dialog': { props: ['modelValue'], template: '<div v-if="modelValue"><slot /></div>' },
  'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
  'el-dropdown-item': { template: '<div><slot /></div>' },
  'el-dropdown-menu': { template: '<div><slot /></div>' },
  'el-button-group': { template: '<div><slot /></div>' },
  'el-icon': { template: '<i><slot /></i>' },
  'el-option': true,
  'el-select': true,
  'el-statistic': true,
  BaseChart: true,
  EmptyState: true,
  PrismLoading: true,
}

function mountReviewTaskList(createTime: string): Promise<VueWrapper> {
  api.tasks.mockResolvedValue(pageOf([{ id: 231, task_name: '任务231', status: 'success', create_time: createTime }]))
  wrapper = mount(ReviewTaskList, scanMountOptions)
  return flushPromises().then(() => wrapper as VueWrapper)
}

function mountReportList(createTime: string): Promise<VueWrapper> {
  api.reports.mockResolvedValue({
    items: [{
      task_id: 231, can_view_report: true, task_name: '任务231', project_name: '时间口径项目', total_issues: 1,
      score: 80, status: 'success', create_time: createTime,
      coverage: { coverage_status: 'complete', score_available: true },
      source: { type: 'security', stats_basis: 'review_issues' },
    }],
    total: 1, page: 1, page_size: 20, pages: 1,
  })
  wrapper = mount(ReportList, {
    global: {
      stubs: {
        'el-card': { template: '<div><slot /></div>' },
        'el-select': { template: '<div><slot /></div>' }, 'el-option': true,
        'el-date-picker': true, 'el-pagination': true,
        'el-checkbox': { props: ['modelValue'], template: '<label><slot /></label>' },
        'el-tooltip': { template: '<div><slot /></div>' },
        'el-button': { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /></button>' },
        'el-icon': { template: '<i><slot /></i>' },
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-dropdown-item': { template: '<div><slot /></div>' },
        'el-tag': { template: '<span><slot /></span>' },
        EmptyState: { props: ['description'], template: '<div class="empty-state">{{ description }}</div>' },
      },
      directives: { loading: {} },
    },
  })
  return flushPromises().then(() => wrapper as VueWrapper)
}

function mountReportDetail(createTime: string): Promise<VueWrapper> {
  api.reportDetail.mockResolvedValue({
    project: { project_name: '时间口径项目', language: 'python' },
    task: { task_name: '任务231', review_type: 'security', total_files: 1, create_time: createTime },
    stats: { score: 80, total_issues: 0 },
    files: [],
    rules_snapshot: [],
  })
  wrapper = shallowMount(ReportDetail, {
    global: { plugins: [ElementPlus], stubs: ReportDetailStubs, directives: { loading: () => undefined } },
  })
  return flushPromises().then(() => wrapper as VueWrapper)
}

async function mountProjectDetail(createTime: string): Promise<VueWrapper> {
  api.projectDetail.mockResolvedValue({
    id: 162, project_name: '时间口径项目', status: 'active', file_count: 1, language: 'python',
    can_update: true, can_delete: true, can_execute: true,
    create_time: createTime, update_time: createTime,
    recent_tasks: [{
      id: 231, review_type: 'security', score: 80, total_issues: 1, status: 'success', create_time: createTime,
    }],
  })
  route.params = { id: '162' }
  wrapper = mount(ProjectDetail, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus],
      stubs: { MetaGPTOrchestrationPanel: true, AgentDiscussionPanel: true, AiPromptModal: true, SecurityScanModal: true },
    },
  })
  await flushPromises()
  ;(wrapper.vm.$ as unknown as { setupState: Record<string, unknown> }).setupState.activeTab = 'tasks'
  await flushPromises()
  return wrapper
}

describe('同一审查任务在各页面显示同一北京时间', () => {
  it.each(WIRE_SHAPES)('审查任务列表：%s', async (shape) => {
    const page = await mountReviewTaskList(shape)
    expect(page.get('.tc-line2').text()).toContain(EXPECTED_MINUTE)
  })

  it.each(WIRE_SHAPES)('报告列表：%s', async (shape) => {
    const page = await mountReportList(shape)
    expect(page.text()).toContain(EXPECTED_MINUTE)
    expect(page.text()).not.toContain('2026-10-22 08:20')
  })

  it.each(WIRE_SHAPES)('报告详情封面：%s', async (shape) => {
    const page = await mountReportDetail(shape)
    expect(page.get('.cover-date').text()).toBe(EXPECTED_MINUTE)
  })

  it.each(WIRE_SHAPES)('项目详情“最近任务”：%s', async (shape) => {
    route.params = { id: '162' }
    const page = await mountProjectDetail(shape)
    const card = page.get('.recent-task-card')
    expect(card.get('time').text()).toBe(EXPECTED_MINUTE)
  })

  it('四个页面对同一输入给出完全相同的文字', async () => {
    const shown: string[] = []
    for (const mountPage of [mountReviewTaskList, mountReportList, mountReportDetail, mountProjectDetail]) {
      const page = await mountPage('2026-10-22T08:20:00')
      const text = mountPage === mountReviewTaskList ? page.get('.tc-line2').text()
        : mountPage === mountReportDetail ? page.get('.cover-date').text()
          : mountPage === mountProjectDetail ? page.get('.recent-task-card time').text()
            : page.text()
      shown.push(text.includes(EXPECTED_MINUTE) ? EXPECTED_MINUTE : text)
      page.unmount()
      wrapper = undefined
      document.body.innerHTML = ''
    }
    expect(shown).toEqual([EXPECTED_MINUTE, EXPECTED_MINUTE, EXPECTED_MINUTE, EXPECTED_MINUTE])
  })
})
