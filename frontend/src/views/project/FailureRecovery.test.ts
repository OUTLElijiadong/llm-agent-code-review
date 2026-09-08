import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'

const api = vi.hoisted(() => ({ getProjects: vi.fn(), createProject: vi.fn(), updateProject: vi.fn(), deleteProject: vi.fn(), analyzeFolder: vi.fn(), getProjectDetail: vi.fn(), getAuditSourceArchiveResult: vi.fn(), deleteSourceRevision: vi.fn(), downloadProjectSource: vi.fn(), uploadAuditSourceArchive: vi.fn(), queueRemoteProjectImport: vi.fn(), getRemoteProjectImport: vi.fn(), cancelRemoteProjectImport: vi.fn() }))
const fileApi = vi.hoisted(() => ({ list: vi.fn(), downloadBinary: vi.fn(), upload: vi.fn(), uploadFolder: vi.fn() }))
const members = vi.hoisted(() => ({ listProjectMembers: vi.fn(), addProjectMember: vi.fn(), updateProjectMemberRole: vi.fn(), removeProjectMember: vi.fn() }))
const agentApi = vi.hoisted(() => ({ listRuntimeAgents: vi.fn(), getSituation: vi.fn(), listTypeMappings: vi.fn(), listAgentSkills: vi.fn() }))
const message = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }))
const reportApi = vi.hoisted(() => ({ getReportDetail: vi.fn(), generateReport: vi.fn(), previewReport: vi.fn(), exportReport: vi.fn() }))
vi.mock('@/api/project', () => api)
vi.mock('@/api/codeFile', () => fileApi)
vi.mock('@/api/projectMember', () => members)
vi.mock('@/api/agent', () => agentApi)
vi.mock('@/api/report', () => reportApi)
vi.mock('@/api/review', () => ({ getTaskIssues: vi.fn() }))
vi.mock('@/utils/agentEventStream', () => ({ subscribeAgentEvents: () => ({ close: vi.fn() }) }))
vi.mock('vue-router', async original => ({ ...await original<typeof import('vue-router')>(), useRouter: () => ({ push: vi.fn(), back: vi.fn() }), useRoute: () => ({ params: { id: '162' }, query: {}, path: '/agents' }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: message }))
import ProjectList from './ProjectList.vue'
import ProjectForm from './ProjectForm.vue'
import ProjectDetail from './ProjectDetail.vue'
import CodeFileList from '../code/CodeFileList.vue'
import AgentCenter from '../agent/AgentCenter.vue'
import ReportDetail from '../report/ReportDetail.vue'
import { mustDiscardReadSnapshot } from '@/composables/withFeedback'

let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
const project = { id: 162, project_name: '原有项目', file_count: 1, language: 'python', can_update: true, can_delete: true, status: 'active', recent_tasks: [], source_revisions: [] }
const file = { id: 1892, file_name: '原有文件.py', language: 'python', version_no: 1, update_time: '2026-09-07', line_count: 1, size_bytes: 8 }
const agent = (code: string) => ({ code, name: code, category: 'reviewer', skills: [], status: 'idle', call_count: 1 })
const fail = (status = 503) => ({ response: { status, data: { message: '合成服务失败' } } })
function deferred<T = any>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function render(component: object, props: Record<string, unknown> = {}) {
  wrapper = mount(component, { props, attachTo: document.body, global: { plugins: [ElementPlus, pinia], stubs: { MetaGPTOrchestrationPanel: true, AgentDiscussionPanel: true, AiPromptModal: true, SecurityScanModal: true } } })
  return wrapper
}
function state(target = wrapper): any { return (target.vm.$ as unknown as { setupState: Record<string, any> }).setupState }
function button(text: string) { const result = wrapper.findAll('button').find(item => item.text() === text); if (!result) throw Error(`缺少 ${text}`); return result }
beforeEach(() => {
  vi.resetAllMocks()
  pinia = createPinia(); setActivePinia(pinia)
  useUserStore().profile = { id: 104, username: 'fixture', role: 'user', status: 1 }
  useUserStore().permissions = new Set(['project:view', 'project:create', 'project:update', 'project:delete', 'project:member:manage', 'file:view', 'file:upload', 'file:download', 'agent:view', 'report:view', 'report:export:json'])
  localStorage.clear()
  api.getProjects.mockResolvedValue({ items: [{ ...project }], total: 1 })
  api.getProjectDetail.mockResolvedValue({ ...project })
  fileApi.list.mockResolvedValue({ items: [file], total: 1 })
  members.listProjectMembers.mockResolvedValue([])
  agentApi.listRuntimeAgents.mockResolvedValue([agent('A'), agent('B')])
  agentApi.getSituation.mockResolvedValue({ online: 2, working: 0, idle: 2, today_calls: 0, spectrum: [], hotspots: [] })
  agentApi.listTypeMappings.mockResolvedValue([])
  agentApi.listAgentSkills.mockResolvedValue([])
  reportApi.getReportDetail.mockResolvedValue({ project, task: { task_name: '已读报告' }, stats: { total_issues: 0 }, files: [], rules_snapshot: [] })
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = '' })

describe('主页面失败恢复真实挂载', () => {
  it('项目刷新503保留快照并显示失败；重试成功后移除提示', async () => {
    render(ProjectList); await flushPromises()
    api.getProjects.mockRejectedValueOnce(fail())
    await state().fetchProjects(); await flushPromises()
    expect(wrapper.text()).toContain('原有项目')
    expect(wrapper.get('[data-testid="project-load-error"]').text()).toContain('上次成功')
    await button('重试读取').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="project-load-error"]').exists()).toBe(false)
  })
  it('项目先失败不能显示还没有项目；403清空已授权快照', async () => {
    api.getProjects.mockRejectedValueOnce(fail())
    render(ProjectList); await flushPromises()
    expect(wrapper.text()).not.toContain('还没有项目')
    await state().fetchProjects()
    api.getProjects.mockRejectedValueOnce(fail(403))
    await state().fetchProjects(); await flushPromises()
    expect(state().projects).toEqual([])
    expect(state().loading).toBe(false)
  })
  it('旧筛选响应不覆盖新结果；新筛选失败明确旧条件快照', async () => {
    render(ProjectList); await flushPromises()
    const a = deferred(); const b = deferred()
    api.getProjects.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    state().keyword = 'A'; const first = state().fetchProjects()
    state().keyword = 'B'; const second = state().fetchProjects()
    b.resolve({ items: [{ ...project, project_name: 'B项目' }], total: 1 }); await second
    a.resolve({ items: [{ ...project, project_name: 'A项目' }], total: 1 }); await first
    expect(state().projects[0].project_name).toBe('B项目')
    state().keyword = 'C'; api.getProjects.mockRejectedValueOnce(fail())
    await state().fetchProjects(); await flushPromises()
    expect(wrapper.get('[data-testid="project-load-error"]').text()).toContain('旧筛选')
  })
  it('创建按钮在父请求完成前保持锁定，超时保留输入且不会自动重发', async () => {
    const create = deferred(); api.createProject.mockReturnValueOnce(create.promise)
    render(ProjectList); await flushPromises()
    await wrapper.get('[data-testid="create-project-button"]').trigger('click'); await flushPromises()
    const form = wrapper.findComponent(ProjectForm)
    state(form).form.project_name = '保留输入'; await flushPromises()
    await button('创建项目').trigger('click'); await flushPromises()
    expect(button('创建项目').attributes('disabled')).toBeDefined()
    await state(form).handleSubmit(); await state().onFormSubmit({ project_name: '保留输入' })
    expect(api.createProject).toHaveBeenCalledTimes(1)
    create.reject(new Error('timeout')); await flushPromises()
    expect(state(form).form.project_name).toBe('保留输入')
    expect(wrapper.text()).toContain('核对')
    expect(state().formSubmitting).toBe(false)
    expect(api.createProject).toHaveBeenCalledTimes(1)
  })
  it('分析失败保留手填值，不显示Agent已分析；分析中不可创建', async () => {
    render(ProjectForm, { visible: true, mode: 'create', initialData: null }); await flushPromises()
    Object.assign(state().form, { project_name: '人工名称', description: '人工描述', language: 'python' })
    const request = deferred(); api.analyzeFolder.mockReturnValueOnce(request.promise)
    const pending = state().autoAnalyzeFolder('folder', ['main.py']); await flushPromises()
    expect(button('创建项目').attributes('disabled')).toBeDefined()
    request.reject(fail()); await pending; await flushPromises()
    expect(state().form).toMatchObject({ project_name: '人工名称', description: '人工描述', language: 'python' })
    expect(wrapper.text()).not.toContain('Agent 已分析')
    expect(wrapper.text()).toContain('分析失败')
  })
  it('文件列表503保留内容与重试；过期响应不覆盖新筛选', async () => {
    render(CodeFileList, { projectId: 162 }); await flushPromises()
    fileApi.list.mockRejectedValueOnce(fail()); await state().fetchFiles(); await flushPromises()
    expect(wrapper.text()).toContain('原有文件.py')
    expect(wrapper.get('[data-testid="file-load-error"]').text()).toContain('上次成功')
    const a = deferred(); const b = deferred(); fileApi.list.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    const first = state().fetchFiles(); state().languageFilter = 'java'; const second = state().fetchFiles()
    b.resolve({ items: [{ ...file, file_name: '最新.java' }], total: 1 }); await second
    a.resolve({ items: [file], total: 1 }); await first
    expect(state().files[0].file_name).toBe('最新.java')
  })
  it('详情503保留只读快照并关闭写操作，403清空；重试重新校验权限', async () => {
    render(ProjectDetail); await flushPromises()
    api.getProjectDetail.mockRejectedValueOnce(fail()); await state().fetchDetail(); await flushPromises()
    expect(wrapper.text()).toContain('原有项目')
    expect(wrapper.get('[data-testid="detail-load-error"]').text()).toContain('只读')
    expect(state().canUpload).toBe(false); expect(state().canManageMembers).toBe(false)
    await button('重试读取').trigger('click'); await flushPromises()
    expect(state().canUpload).toBe(true)
    api.getProjectDetail.mockRejectedValueOnce(fail(403)); await state().fetchDetail(); await flushPromises()
    expect(state().project).toBeNull()
    expect(wrapper.text()).not.toContain('项目不存在')
  })
  it('Agent画像读取失败仍更新成功目录，不产生同步成功提示；手动恢复可成功', async () => {
    render(AgentCenter); await flushPromises(); message.success.mockClear()
    agentApi.listRuntimeAgents.mockResolvedValueOnce([agent('最新Agent')])
    agentApi.listTypeMappings.mockRejectedValueOnce(fail())
    await button('刷新').trigger('click'); await flushPromises()
    expect(state().runtime[0].code).toBe('最新Agent')
    expect(wrapper.get('[data-testid="agent-load-error"]').text()).toContain('画像')
    expect(message.success).not.toHaveBeenCalled()
    await button('刷新').trigger('click'); await flushPromises()
    expect(message.success).toHaveBeenCalledWith('已同步最新数据')
  })
  it('Agent技能切换忽略旧响应；失败显示重试而不是没有技能', async () => {
    render(AgentCenter); await flushPromises()
    const a = deferred(); const b = deferred(); agentApi.listAgentSkills.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    state().onAgentSelect('A'); await flushPromises(); state().onAgentSelect('B'); await flushPromises()
    b.resolve([{ name: 'B.skill', type: 'proactive' }]); await flushPromises()
    a.resolve([{ name: 'A.skill', type: 'proactive' }]); await flushPromises()
    expect(state().agentSkills[0].name).toBe('B.skill')
    agentApi.listAgentSkills.mockRejectedValueOnce(fail()); await state().loadAgentSkills('B'); await flushPromises()
    expect(wrapper.get('[data-testid="skills-load-error"]').text()).toContain('技能读取失败')
    expect(state().agentSkills[0].name).toBe('B.skill')
    expect(wrapper.text()).not.toContain('该 Agent 暂未挂载 Skill')
  })
  it('报告首次失败有原地重试，503保留标明过期的报告，权限业务码清空', async () => {
    reportApi.getReportDetail.mockRejectedValueOnce(fail())
    render(ReportDetail); await flushPromises()
    expect(wrapper.get('[data-testid="report-load-error"]').text()).toContain('报告读取失败')
    await button('重试读取报告').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('已读报告')
    reportApi.getReportDetail.mockRejectedValueOnce(fail()); await state().loadReport(); await flushPromises()
    expect(wrapper.text()).toContain('已读报告')
    expect(wrapper.get('[data-testid="report-load-error"]').text()).toContain('上次成功')
    expect(state().canExport('json')).toBe(false)
    reportApi.getReportDetail.mockRejectedValueOnce({ code: 40300, message: '拒绝访问' }); await state().loadReport(); await flushPromises()
    expect(state().report).toBeNull()
  })
  it('二进制本地异常有持续反馈且回收URL，无真实下载且可以手动重试', async () => {
    fileApi.list.mockResolvedValue({ items: [{ ...file, is_binary: 1 }], total: 1 })
    fileApi.downloadBinary.mockResolvedValue(new Blob(['fixture']))
    URL.createObjectURL = vi.fn(() => 'blob:local-fixture')
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error('本地浏览器限制') })
    render(CodeFileList, { projectId: 162 }); await flushPromises()
    await button('下载').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-testid="file-download-error"]').text()).toContain('下载未完成')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-fixture')
    expect(document.querySelector('a[download]')).toBeNull()
    expect(state().downloadingId).toBeNull()
    expect(fileApi.downloadBinary).toHaveBeenCalledTimes(1)
    click.mockImplementation(() => {})
    await button('下载').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="file-download-error"]').exists()).toBe(false)
    expect(fileApi.downloadBinary).toHaveBeenCalledTimes(2)
  })
  it.each([40101, 40102, 40300, 40301, 40400])('真实拦截器解包业务码%s不可保留受保护快照', code => {
    expect(mustDiscardReadSnapshot({ code, message: '合成错误' })).toBe(true)
  })
  it.each([42900, 50000, 50301])('临时失败%s允许保留已成功快照', code => {
    expect(mustDiscardReadSnapshot({ code, message: '临时错误' })).toBe(false)
  })
  it('所有已读页面对解包403清空快照', async () => {
    for (const [component, props, request, handler, field] of [
      [ProjectList, {}, api.getProjects, 'fetchProjects', 'projects'],
      [ProjectDetail, {}, api.getProjectDetail, 'fetchDetail', 'project'],
      [CodeFileList, { projectId: 162 }, fileApi.list, 'fetchFiles', 'files'],
      [AgentCenter, {}, agentApi.listRuntimeAgents, 'refreshAll', 'runtime'],
    ] as const) {
      render(component, props); await flushPromises()
      request.mockRejectedValueOnce({ code: 40300, message: '权限已撤销' })
      await state()[handler](); await flushPromises()
      expect(state()[field] ?? []).toEqual([])
      wrapper.unmount()
    }
  })
  it('卸载后的迟到响应不能重新写状态或注册Agent订阅心跳', async () => {
    const pending = deferred(); agentApi.listRuntimeAgents.mockReturnValueOnce(pending.promise)
    render(AgentCenter); await flushPromises()
    const saved = state(); wrapper.unmount()
    pending.resolve([agent('迟到')]); await flushPromises()
    expect(saved.runtime).toEqual([])
  })
  it('Agent手动和心跳同时刷新只发一组GET；普通角色不能调用自进化', async () => {
    render(AgentCenter); await flushPromises()
    const pending = deferred(); agentApi.listRuntimeAgents.mockReturnValueOnce(pending.promise)
    const first = state().refreshAll(); const second = state().refreshAgentStats()
    expect(agentApi.listRuntimeAgents).toHaveBeenCalledTimes(2) // 首次挂载 + 本轮一次
    pending.resolve([agent('新快照')]); await Promise.all([first, second])
    expect(state().loading).toBe(false)
    await state().triggerSelfImprove(agent('新快照'))
    expect(state().triggering).toBe(false)
  })
  it('关闭分析中的表单后迟到结果不覆盖输入或显示成功', async () => {
    render(ProjectForm, { visible: true, mode: 'create', initialData: null }); await flushPromises()
    state().form.project_name = '手动项目'
    const pending = deferred(); api.analyzeFolder.mockReturnValueOnce(pending.promise)
    const action = state().autoAnalyzeFolder('folder', ['main.py']); await flushPromises()
    await button('取消').trigger('click')
    pending.resolve({ project_name: '迟到建议', description: '旧内容', language: 'python', language_name: 'Python' })
    await action; await flushPromises()
    expect(state().form.project_name).toBe('手动项目')
    expect(message.success).not.toHaveBeenCalled()
    expect(wrapper.emitted('update:visible')).toContainEqual([false])
  })
  it('创建成功但上传未完成仍锁定；上传失败只提示补传而不重建项目', async () => {
    api.createProject.mockResolvedValueOnce({ id: 200 })
    const upload = deferred(); fileApi.uploadFolder.mockReturnValueOnce(upload.promise)
    render(ProjectList); await flushPromises()
    const action = state().onFormSubmit({ project_name: '已有新项目', files: [new File(['qa'], 'qa.py')] }); await flushPromises()
    expect(state().formSubmitting).toBe(true)
    await state().onFormSubmit({ project_name: '已有新项目' })
    expect(api.createProject).toHaveBeenCalledTimes(1)
    upload.reject(new Error('上传中断')); await action; await flushPromises()
    expect(state().formSubmitting).toBe(false)
    expect(api.createProject).toHaveBeenCalledTimes(1)
    expect(message.error).toHaveBeenCalled()
  })
})
