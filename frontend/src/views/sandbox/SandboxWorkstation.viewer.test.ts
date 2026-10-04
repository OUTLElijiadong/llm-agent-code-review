import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'

const api = vi.hoisted(() => ({ projects: vi.fn(), detail: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), authorize: vi.fn(), stop: vi.fn(), extend: vi.fn(), preview: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: api.projects, getProjectDetail: api.detail }))
vi.mock('@/api/sandbox', () => ({ listSandboxes: api.list, getSandbox: api.get, createSandbox: api.create,
  authorizeSandboxRemoteTarget: api.authorize, stopSandbox: api.stop, extendSandbox: api.extend,
  createSandboxPreviewSession: api.preview, downloadSandboxArtifact: vi.fn(), searchSandboxCapabilities: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/mcpGovernance', () => ({ listSandboxWorkers: vi.fn().mockResolvedValue([]) }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: api.confirm } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
import SandboxWorkstation from './SandboxWorkstation.vue'

const project = { id: 7, project_name: '本地权限样本', language: 'python', status: 'active', can_execute: true }
const environment = { public_id: 'local-capability', project_id: 7, owner_id: 11, agent_code: 'local', purpose: 'deploy', language: 'python', test_mode: 'deploy', status: 'ready', runtime: 'local-mock', source_sha256: 'a'.repeat(64), expires_at: '2026-10-05', result: {}, events: [], artifacts: [], preview_path: '/api/sandboxes/local-capability/preview/', can_execute: true, can_preview: true, can_stop: true }
let user: ReturnType<typeof useUserStore>
const wrappers: ReturnType<typeof shallowMount>[] = []
beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  user = useUserStore()
  user.token = 'local-sandbox-owner'
  user.profile = { id: 11, username: 'local', role: 'user', status: 1 }
  user.permissions = new Set(['project:view', 'file:view'])
  api.projects.mockResolvedValue({ items: [{ ...project }], total: 1 })
  api.detail.mockResolvedValue({ source_revisions: [] })
  api.list.mockResolvedValue([{ ...environment }])
  api.get.mockResolvedValue({ ...environment })
  api.create.mockResolvedValue({ ...environment, public_id: 'created-local' })
  api.extend.mockResolvedValue({ ...environment })
  api.stop.mockResolvedValue({ ...environment, status: 'stopped' })
  api.preview.mockResolvedValue({ path: environment.preview_path })
  api.confirm.mockResolvedValue(true)
  vi.spyOn(window, 'open').mockReturnValue(null)
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })
async function render() {
  const slot = { template: '<div><slot /></div>' }
  const wrapper = shallowMount(SandboxWorkstation, { global: { directives: { loading: () => {} }, stubs: {
    'el-form': slot, 'el-form-item': slot, 'el-select': slot, 'el-option': true,
    'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' },
    'el-alert': { props: ['title'], template: '<div>{{ title }}<slot /></div>' },
    'el-tag': slot, 'el-icon': slot, 'el-checkbox': slot, 'el-empty': true, 'el-input': true, 'el-input-number': true, 'el-radio-group': slot, 'el-radio-button': slot,
  } } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, vm: wrapper.vm as any }
}

describe('R3 沙箱只读资源门（本地 API 桩，不执行任务）', () => {
  it('R3 审查员非创建者可按独立预览能力查看共享运行环境，不能续期', async () => {
    user.profile = { id: 11, username: 'local-reviewer', role: 'reviewer', status: 1 }
    api.list.mockResolvedValue([{ ...environment, owner_id: 12, can_execute: false, can_preview: true, can_stop: false }])
    const { vm, wrapper } = await render()
    expect(wrapper.findAll('button').some(button => button.text() === '打开预览')).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === '续期 24h')).toBe(false)
    await vm.openPreview()
    expect(api.preview).toHaveBeenCalledExactlyOnceWith('local-capability')
    expect(window.open).toHaveBeenLastCalledWith(`${window.location.origin}${environment.preview_path}`, '_blank', 'noopener,noreferrer')
  })

  it.each([false, undefined])('R3 预览能力拒绝或缺值时不因可执行而打开预览 %s', async canPreview => {
    api.list.mockResolvedValue([{ ...environment, can_execute: true, can_preview: canPreview }])
    const { vm, wrapper } = await render()
    expect(wrapper.findAll('button').some(button => button.text() === '打开预览')).toBe(false)
    await vm.openPreview()
    expect(api.preview).not.toHaveBeenCalled()
    expect(window.open).not.toHaveBeenCalled()
  })

  it('R3 viewer 的预览拒绝能力保持只读，不调用预览接口', async () => {
    api.list.mockResolvedValue([{ ...environment, can_execute: false, can_preview: false }])
    const { vm } = await render()
    await vm.openPreview()
    expect(api.preview).not.toHaveBeenCalled()
    expect(window.open).not.toHaveBeenCalled()
  })

  it('R3 独立预览能力仍与当前全局文件读取权限相交', async () => {
    api.list.mockResolvedValue([{ ...environment, can_execute: false, can_preview: true }])
    const { vm } = await render()
    user.permissions.delete('file:view')
    await vm.openPreview()
    expect(api.preview).not.toHaveBeenCalled()
    expect(window.open).not.toHaveBeenCalled()
  })

  it('R3 预览能力撤销再授予后，旧会话回执不会导航空窗口', async () => {
    let resolve!: (value: { path: string }) => void
    api.preview.mockReturnValueOnce(new Promise<{ path: string }>(done => { resolve = done }))
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.mocked(window.open).mockReturnValueOnce(previewWindow as any)
    api.list.mockResolvedValue([{ ...environment, can_preview: true }])
    const { vm } = await render()
    const request = vm.openPreview()
    vm.environments[0].can_preview = false
    vm.environments[0].can_preview = true
    resolve({ path: environment.preview_path }); await request
    expect(previewWindow.location.replace).not.toHaveBeenCalled()
    expect(previewWindow.close).toHaveBeenCalledOnce()
  })

  it('R3 仅撤销创建与续期能力不取消仍然合法的共享预览', async () => {
    let resolve!: (value: { path: string }) => void
    api.preview.mockReturnValueOnce(new Promise<{ path: string }>(done => { resolve = done }))
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.mocked(window.open).mockReturnValueOnce(previewWindow as any)
    api.list.mockResolvedValue([{ ...environment, can_preview: true }])
    const { vm } = await render()
    const request = vm.openPreview()
    vm.environments[0].can_execute = false
    await flushPromises()
    expect(vm.mutating).toBe(true)
    resolve({ path: environment.preview_path }); await request
    expect(previewWindow.location.replace).toHaveBeenCalledExactlyOnceWith(`${window.location.origin}${environment.preview_path}`)
    expect(previewWindow.close).not.toHaveBeenCalled()
  })

  it.each(['全局权限撤销再授予', '切换账号', '切换环境', '环境已关闭', '卸载页面'] as const)('R3 会话等待期间%s不导航旧预览窗口', async scenario => {
    let resolve!: (value: { path: string }) => void
    api.preview.mockReturnValueOnce(new Promise<{ path: string }>(done => { resolve = done }))
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.mocked(window.open).mockReturnValueOnce(previewWindow as any)
    const { vm, wrapper } = await render()
    const request = vm.openPreview()
    if (scenario === '全局权限撤销再授予') {
      user.permissions.delete('file:view')
      user.permissions.add('file:view')
    } else if (scenario === '切换账号') user.token = 'local-other-account'
    else if (scenario === '切换环境') vm.selectedId = 'another-environment'
    else if (scenario === '环境已关闭') vm.environments[0].status = 'stopped'
    else wrapper.unmount()
    resolve({ path: environment.preview_path }); await request
    expect(previewWindow.location.replace).not.toHaveBeenCalled()
    expect(previewWindow.close).toHaveBeenCalledOnce()
  })

  it('R3 当前有效预览失败关闭空窗口，保留权限并允许原场景重试', async () => {
    api.preview.mockRejectedValueOnce(new Error('local preview failure'))
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.mocked(window.open).mockReturnValueOnce(previewWindow as any)
    const { vm } = await render()
    await vm.openPreview()
    expect(previewWindow.close).toHaveBeenCalledOnce()
    expect(vm.mutating).toBe(false)
    await vm.openPreview()
    expect(api.preview).toHaveBeenCalledTimes(2)
    expect(window.open).toHaveBeenLastCalledWith(`${window.location.origin}${environment.preview_path}`, '_blank', 'noopener,noreferrer')
  })

  it.each([false, undefined])('不可执行或能力缺值的项目不能创建或申请远程授权 %s', async canExecute => {
    api.projects.mockResolvedValue({ items: [{ ...project, can_execute: canExecute }], total: 1 })
    const { vm } = await render()
    vm.form.project_id = 7
    expect(vm.submitDisabled).toBe(true)
    await vm.submit()
    expect(api.create).not.toHaveBeenCalled()
    expect(api.authorize).not.toHaveBeenCalled()
  })

  it.each([false, undefined])('已有仅可读环境不显示执行入口且直接调用不会打开窗口 %s', async canExecute => {
    api.list.mockResolvedValue([{ ...environment, owner_id: 12, can_execute: canExecute, can_preview: canExecute, can_stop: false }])
    const { vm, wrapper } = await render()
    for (const label of ['打开预览', '续期 24h', '关闭']) expect(wrapper.findAll('button').some(button => button.text() === label)).toBe(false)
    await vm.openPreview(); await vm.extendCurrent(); await vm.stopCurrent()
    expect(api.preview).not.toHaveBeenCalled()
    expect(api.extend).not.toHaveBeenCalled()
    expect(api.stop).not.toHaveBeenCalled()
    expect(api.confirm).not.toHaveBeenCalled()
    expect(window.open).not.toHaveBeenCalled()
  })

  it('项目可执行仍须当前账号的全局文件权限', async () => {
    const { vm } = await render()
    user.permissions.delete('file:view')
    await flushPromises()
    expect(vm.submitDisabled).toBe(true)
    await vm.submit(); await vm.openPreview(); await vm.extendCurrent()
    expect(api.create).not.toHaveBeenCalled()
    expect(api.preview).not.toHaveBeenCalled()
    expect(api.extend).not.toHaveBeenCalled()
  })

  it('合法执行能力和全局权限保留创建与续期入口', async () => {
    const { vm } = await render()
    expect(vm.submitDisabled).toBe(false)
    await vm.submit()
    expect(api.create).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ project_id: 7 }))
    await vm.extendCurrent()
    expect(api.extend).toHaveBeenCalledExactlyOnceWith('created-local', 24)
  })

  it('R3 自有环境降为 viewer 且失去执行权限，仍能按服务端回收能力关闭', async () => {
    user.permissions = new Set(['project:view'])
    api.list.mockResolvedValue([{ ...environment, can_execute: false, can_stop: true }])
    const { vm, wrapper } = await render()
    expect(wrapper.findAll('button').some(button => button.text() === '关闭')).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === '打开预览')).toBe(false)
    await vm.stopCurrent()
    expect(api.stop).toHaveBeenCalledExactlyOnceWith('local-capability')
  })

  it.each([false, undefined])('R3 可执行但关闭能力拒绝或缺值时不能关闭 %s', async canStop => {
    api.list.mockResolvedValue([{ ...environment, can_stop: canStop }])
    const { vm, wrapper } = await render()
    expect(wrapper.findAll('button').some(button => button.text() === '关闭')).toBe(false)
    await vm.stopCurrent()
    expect(api.stop).not.toHaveBeenCalled()
    expect(api.confirm).not.toHaveBeenCalled()
  })

  it('R3 普通管理员只看到服务器关闭拒绝能力，不能关闭他人环境', async () => {
    user.profile = { id: 11, username: 'local-admin', role: 'admin', status: 1 }
    api.list.mockResolvedValue([{ ...environment, owner_id: 12, can_stop: false }])
    const { vm } = await render()
    await vm.stopCurrent()
    expect(api.stop).not.toHaveBeenCalled()
  })

  it('R3 回收确认期间切换账号不关闭旧环境', async () => {
    let resolve!: () => void
    api.confirm.mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { vm } = await render()
    const request = vm.stopCurrent()
    user.token = 'other-account'
    resolve(); await request
    expect(api.stop).not.toHaveBeenCalled()
  })

  it('R3 最高管理员的服务端回收能力可关闭他人环境，普通管理员不由前端推断授权', async () => {
    user.profile = { id: 11, username: 'local-root', role: 'super_admin', status: 1 }
    user.permissions = new Set()
    api.list.mockResolvedValue([{ ...environment, owner_id: 12, can_execute: false, can_stop: true }])
    const { vm } = await render()
    await vm.stopCurrent()
    expect(api.stop).toHaveBeenCalledExactlyOnceWith('local-capability')
  })

  it('R3 回收确认期间撤销再授予关闭能力，旧确认仍作废', async () => {
    let resolve!: () => void
    api.confirm.mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { vm } = await render()
    const request = vm.stopCurrent()
    vm.environments[0].can_stop = false
    vm.environments[0].can_stop = true
    resolve(); await request
    expect(api.stop).not.toHaveBeenCalled()
  })

  it('R3 仅撤销执行权限不取消已确认的自有资源回收', async () => {
    let resolve!: () => void
    api.confirm.mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { vm } = await render()
    const request = vm.stopCurrent()
    expect(vm.mutating).toBe(true)
    user.permissions.delete('file:view')
    await flushPromises()
    expect(vm.mutating).toBe(true)
    resolve(); await request
    expect(api.stop).toHaveBeenCalledExactlyOnceWith('local-capability')
  })
})
