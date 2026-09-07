import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'

const projectApi = vi.hoisted(() => ({ getProjectDetail: vi.fn(), getAuditSourceArchiveResult: vi.fn(), deleteSourceRevision: vi.fn(), downloadProjectSource: vi.fn(), uploadAuditSourceArchive: vi.fn() }))
const fileApi = vi.hoisted(() => ({ list: vi.fn(), getDetail: vi.fn(), update: vi.fn(), downloadBinary: vi.fn(), upload: vi.fn(), uploadFolder: vi.fn(), listVersions: vi.fn(), getVersion: vi.fn(), restoreVersion: vi.fn() }))
const membersApi = vi.hoisted(() => ({ listProjectMembers: vi.fn(), addProjectMember: vi.fn(), updateProjectMemberRole: vi.fn(), removeProjectMember: vi.fn() }))
const router = vi.hoisted(() => ({ push: vi.fn(), back: vi.fn() }))
const confirm = vi.hoisted(() => vi.fn().mockResolvedValue(true))
vi.mock('@/api/project', () => projectApi)
vi.mock('@/api/codeFile', () => fileApi)
vi.mock('@/api/projectMember', () => membersApi)
vi.mock('vue-router', async (importOriginal) => ({ ...await importOriginal<typeof import('vue-router')>(), useRoute: () => ({ params: { id: '162', projectId: '999', fileId: '1892' } }), useRouter: () => router }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), info: vi.fn() } }))
vi.mock('@/components/editor/MonacoEditor.vue', () => ({ default: { props: ['readonly'], template: '<textarea class="editor-probe" :readonly="readonly" />' } }))
import ProjectDetail from './ProjectDetail.vue'
import CodeEditor from '../code/CodeEditor.vue'
import VersionHistory from '../code/VersionHistory.vue'
import CodeFileList from '../code/CodeFileList.vue'

const fullPermissions = ['project:view', 'file:view', 'file:upload', 'file:edit', 'file:download', 'project:delete', 'project:member:manage']
const reviewer = { id: 2, user_id: 104, username: 'qa_reviewer', role_in_project: 'reviewer', create_time: '2026-09-07T00:00:00' }
const project = (write: boolean) => ({ id: 162, project_name: '权限挂载项目', language: 'python', file_count: 1, source_mode: 'files', can_update: write, can_delete: write, recent_tasks: [], source_revisions: [], status: 'active', create_time: '2026-09-07T00:00:00', update_time: '2026-09-07T00:00:00' })
const file = { id: 1892, project_id: 162, file_name: 'qa.py', content: 'print(1)', language: 'python', version_no: 1, is_binary: 0, size_bytes: 8, update_time: '2026-09-07T00:00:00' }
let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
function render(component: object) {
  wrapper = mount(component, { attachTo: document.body, global: { plugins: [ElementPlus, pinia], stubs: {
    AiPromptModal: { props: ['modelValue'], template: '<div class="ai-modal" :data-visible="modelValue" />' },
    SecurityScanModal: { props: ['modelValue'], template: '<div class="scan-modal" :data-visible="modelValue" />' },
    MonacoEditor: { props: ['readonly'], template: '<textarea class="editor-probe" :readonly="readonly" />' },
  } } })
  return wrapper
}
function buttons(label: string) { return wrapper.findAll('button').filter(item => item.text() === label) }
function button(label: string) { const found = buttons(label)[0]; if (!found) throw Error(`缺少按钮 ${label}`); return found }
// 页面和 Element Plus 按钮/表格/对话框真实挂载；只隔离网络与 Monaco/模型模态框。
function handlers() { return wrapper.vm as unknown as Record<string, (...args: never[]) => Promise<void>> }
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia)
  useUserStore().permissions = new Set(fullPermissions)
  projectApi.getProjectDetail.mockResolvedValue(project(true))
  membersApi.listProjectMembers.mockResolvedValue([reviewer])
  fileApi.list.mockResolvedValue({ items: [file], total: 1 })
  fileApi.getDetail.mockResolvedValue(file)
  fileApi.listVersions.mockResolvedValue({ items: [{ version_no: 1, create_time: file.update_time }], total: 1 })
  fileApi.getVersion.mockResolvedValue({ content: 'print(1)', version_no: 1 })
  fileApi.update.mockResolvedValue({ version_no: 2 })
  fileApi.upload.mockResolvedValue({ quarantined: false })
  fileApi.uploadFolder.mockResolvedValue({ success_count: 1, fail_count: 0 })
  projectApi.downloadProjectSource.mockResolvedValue(new Blob(['qa']))
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  URL.createObjectURL = vi.fn(() => 'blob:qa'); URL.revokeObjectURL = vi.fn()
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = '' })

describe('项目详情资源角色与 RBAC 求交', () => {
  it('reviewer 隐藏全部写入口，保留合法下载和 AI 修复手册，直接调用写 handler 无效', async () => {
    projectApi.getProjectDetail.mockResolvedValue(project(false))
    render(ProjectDetail); await flushPromises()
    for (const label of ['上传文件', '上传文件夹', '上传审计包', '添加成员', '移除', '🛡 安全审计']) expect(buttons(label)).toHaveLength(0)
    expect(wrapper.find('#pane-members .el-select').exists()).toBe(false)
    await button('下载源码').trigger('click'); await flushPromises()
    expect(projectApi.downloadProjectSource).toHaveBeenCalledExactlyOnceWith(162)
    await button('AI 修复手册').trigger('click'); await flushPromises()
    expect(wrapper.get('.ai-modal').attributes('data-visible')).toBe('true')
    await handlers().submitAddMember()
    await handlers().handleChangeRole(104 as never, 'owner' as never)
    await handlers().handleRemoveMember(reviewer as never)
    await handlers().removeRevision({ id: 1, revision_no: 1 } as never)
    for (const input of wrapper.findAll('input[type=file]')) {
      Object.defineProperty(input.element, 'files', { configurable: true, value: [new File(['print(1)'], 'qa.py')] })
      await input.trigger('change')
    }
    await flushPromises()
    for (const request of [fileApi.upload, fileApi.uploadFolder, projectApi.uploadAuditSourceArchive, projectApi.deleteSourceRevision, membersApi.addProjectMember, membersApi.updateProjectMemberRole, membersApi.removeProjectMember]) expect(request).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('owner 具备对应权限时真实按钮能上传、添加成员、修改角色和移除', async () => {
    render(ProjectDetail); await flushPromises()
    expect(buttons('上传文件')).toHaveLength(1)
    await wrapper.get('#tab-members').trigger('click')
    await button('添加成员').trigger('click'); await flushPromises()
    const dialog = wrapper.findComponent({ name: 'ElDialog' })
    const idInput = dialog.find('input'); await idInput.setValue('105')
    await button('确认添加').trigger('click'); await flushPromises()
    expect(membersApi.addProjectMember).toHaveBeenCalledExactlyOnceWith(162, { user_id: 105, role_in_project: 'reviewer' })
    const select = wrapper.find('#pane-members').findComponent({ name: 'ElSelect' })
    select.vm.$emit('change', 'owner'); await flushPromises()
    expect(membersApi.updateProjectMemberRole).toHaveBeenCalledExactlyOnceWith(162, 104, { role_in_project: 'owner' })
    await button('移除').trigger('click'); await flushPromises()
    expect(membersApi.removeProjectMember).toHaveBeenCalledExactlyOnceWith(162, 104)
    const input = wrapper.findAll('input[type=file]')[0]
    Object.defineProperty(input.element, 'files', { configurable: true, value: [new File(['print(1)'], 'qa.py')] })
    await input.trigger('change'); await flushPromises()
    expect(fileApi.upload).toHaveBeenCalledOnce()
  })

  it('owner 被撤回动作权限也不可管理成员、上传、下载或删除副本', async () => {
    useUserStore().permissions = new Set(['project:view', 'file:view'])
    projectApi.getProjectDetail.mockResolvedValue({ ...project(true), source_revisions: [{ id: 7, revision_no: 1, repaired_files: ['qa.py'] }] })
    render(ProjectDetail); await flushPromises()
    for (const label of ['上传文件', '上传文件夹', '添加成员', '移除', '下载源码', '删除']) expect(buttons(label)).toHaveLength(0)
    await handlers().handleDownloadSource(); await handlers().removeRevision({ id: 7, revision_no: 1 } as never)
    expect(projectApi.downloadProjectSource).not.toHaveBeenCalled(); expect(confirm).not.toHaveBeenCalled()
  })

  it('owner 可以上传文件夹、空项目审计包，以及确认删除自己的源码修复副本', async () => {
    projectApi.getProjectDetail.mockResolvedValue({ ...project(true), file_count: 0, source_revisions: [{ id: 7, revision_no: 1, repaired_files: ['qa.py'] }] })
    projectApi.uploadAuditSourceArchive.mockResolvedValue({ malware_status: 'clean', file_count: 1 })
    render(ProjectDetail); await flushPromises()
    await wrapper.get('#tab-files').trigger('click')
    const inputs = wrapper.findAll('input[type=file]')
    for (const [index, label, filename] of [[1, '上传文件夹', 'qa.py'], [2, '上传审计包', 'qa.zip']] as const) {
      const picker = vi.spyOn(inputs[index].element as HTMLInputElement, 'click').mockImplementation(() => {})
      await button(label).trigger('click')
      expect(picker).toHaveBeenCalledOnce()
      Object.defineProperty(inputs[index].element, 'files', { configurable: true, value: [new File(['qa'], filename)] })
      await inputs[index].trigger('change'); await flushPromises()
    }
    expect(fileApi.uploadFolder).toHaveBeenCalledExactlyOnceWith(162, expect.any(Array))
    expect(projectApi.uploadAuditSourceArchive).toHaveBeenCalledExactlyOnceWith(162, expect.any(File))
    await button('删除').trigger('click'); await flushPromises()
    expect(projectApi.deleteSourceRevision).toHaveBeenCalledExactlyOnceWith(162, 7)
  })

  it('确认框打开期间撤销成员权限，确认后不再发送删除请求', async () => {
    let resolve!: (value: boolean) => void
    confirm.mockReturnValueOnce(new Promise(resolvePromise => { resolve = resolvePromise }))
    render(ProjectDetail); await flushPromises()
    await wrapper.get('#tab-members').trigger('click')
    await button('移除').trigger('click')
    useUserStore().permissions = new Set(['project:view'])
    resolve(true); await flushPromises()
    expect(membersApi.removeProjectMember).not.toHaveBeenCalled()
  })

  it('无权限账号不主动读取项目、文件或成员', async () => {
    useUserStore().permissions = new Set()
    render(ProjectDetail); await flushPromises()
    for (const request of [projectApi.getProjectDetail, fileApi.list, membersApi.listProjectMembers]) expect(request).not.toHaveBeenCalled()
  })

  it('有 scan 权限的 reviewer 可以打开扫描，不因没有 security:view 调取保存结果', async () => {
    useUserStore().permissions = new Set([...fullPermissions, 'security:scan'])
    projectApi.getProjectDetail.mockResolvedValue({ ...project(false), source_archive: { audit_status: 'succeeded', max_compression_ratio: 1 } })
    render(ProjectDetail); await flushPromises()
    await button('🛡 安全审计').trigger('click'); await flushPromises()
    expect(wrapper.get('.scan-modal').attributes('data-visible')).toBe('true')
    expect(projectApi.getAuditSourceArchiveResult).not.toHaveBeenCalled()
  })
})

describe('文件编辑与版本恢复真实项目授权', () => {
  it.each([CodeEditor, VersionHistory])('资源元信息返回之前及失败时保持只读', async (component) => {
    let reject!: (reason: unknown) => void
    projectApi.getProjectDetail.mockReturnValue(new Promise((_, rejectPromise) => { reject = rejectPromise }))
    render(component); await flushPromises()
    expect(projectApi.getProjectDetail).toHaveBeenCalledWith(162) // 取文件的真实 project_id，不信路由999。
    expect(buttons('保存 (Ctrl+S)')).toHaveLength(0); expect(buttons('恢复')).toHaveLength(0)
    reject(new Error('metadata failed')); await flushPromises()
    expect(buttons('保存 (Ctrl+S)')).toHaveLength(0); expect(buttons('恢复')).toHaveLength(0)
  })

  it('reviewer 即使有 file:edit，编辑器只读且 Ctrl+S/处理函数不保存', async () => {
    projectApi.getProjectDetail.mockResolvedValue(project(false))
    render(CodeEditor); await flushPromises()
    expect(wrapper.get('.editor-probe').attributes('readonly')).toBeDefined()
    expect(buttons('保存 (Ctrl+S)')).toHaveLength(0)
    window.dispatchEvent(new KeyboardEvent('keydown', { ctrlKey: true, key: 's' }))
    await handlers().handleSave()
    expect(fileApi.update).not.toHaveBeenCalled()
  })

  it('owner 授权后保存真实内容并更新版本', async () => {
    render(CodeEditor); await flushPromises()
    expect(wrapper.get('.editor-probe').attributes('readonly')).toBeUndefined()
    await button('保存 (Ctrl+S)').trigger('click'); await flushPromises()
    expect(fileApi.update).toHaveBeenCalledExactlyOnceWith(1892, { content: 'print(1)' })
    expect(wrapper.text()).toContain('v2')
  })

  it('reviewer 能查看历史，但恢复按钮和处理函数均拒绝', async () => {
    projectApi.getProjectDetail.mockResolvedValue(project(false))
    render(VersionHistory); await flushPromises()
    expect(buttons('恢复')).toHaveLength(0)
    await button('查看').trigger('click'); await flushPromises()
    expect(fileApi.getVersion).toHaveBeenCalledExactlyOnceWith(1892, 1)
    await handlers().handleRestore(1 as never)
    expect(fileApi.restoreVersion).not.toHaveBeenCalled()
  })

  it('owner 可以确认恢复历史版本', async () => {
    render(VersionHistory); await flushPromises()
    await button('恢复').trigger('click'); await flushPromises()
    wrapper.findComponent({ name: 'ElPopconfirm' }).vm.$emit('confirm')
    await flushPromises()
    expect(fileApi.restoreVersion).toHaveBeenCalledExactlyOnceWith(1892, 1)
  })

  it.each([CodeEditor, VersionHistory])('owner 没有 file:edit 时不可保存或恢复', async (component) => {
    useUserStore().permissions = new Set(['project:view', 'file:view'])
    render(component); await flushPromises()
    expect(buttons('保存 (Ctrl+S)')).toHaveLength(0); expect(buttons('恢复')).toHaveLength(0)
    expect(projectApi.getProjectDetail).not.toHaveBeenCalled()
    expect(fileApi.update).not.toHaveBeenCalled(); expect(fileApi.restoreVersion).not.toHaveBeenCalled()
  })

  it('二进制编辑器无下载权限时按钮和处理函数均拒绝', async () => {
    useUserStore().permissions = new Set(['project:view', 'file:view'])
    fileApi.getDetail.mockResolvedValue({ ...file, is_binary: 1 })
    render(CodeEditor); await flushPromises()
    expect(buttons('下载文件')).toHaveLength(0)
    await handlers().handleDownload()
    expect(fileApi.downloadBinary).not.toHaveBeenCalled()
  })

  it('文件列表无下载权限不提供二进制下载', async () => {
    useUserStore().permissions = new Set(['file:view'])
    fileApi.list.mockResolvedValue({ items: [{ ...file, is_binary: 1 }], total: 1 })
    wrapper = mount(CodeFileList, { props: { projectId: 162 }, global: { plugins: [ElementPlus, pinia] } })
    await flushPromises()
    expect(buttons('下载')).toHaveLength(0)
    await handlers().handleDownload(file as never)
    expect(fileApi.downloadBinary).not.toHaveBeenCalled()
    await button('查看元信息').trigger('click')
    expect(router.push).toHaveBeenCalledWith('/code/162/file/1892')
  })
})
