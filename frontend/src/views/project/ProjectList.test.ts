import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const permissionState = vi.hoisted(() => ({ importAllowed: false, createAllowed: false }))
const routerState = vi.hoisted(() => ({ push: vi.fn() }))
const projectApi = vi.hoisted(() => ({
  getProjects: vi.fn(),
  deleteProject: vi.fn(),
  createProject: vi.fn(),
  updateProject: vi.fn(),
  queueRemoteProjectImport: vi.fn(),
  getRemoteProjectImport: vi.fn(),
  cancelRemoteProjectImport: vi.fn(),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    hasPermission: (code: string) => {
      if (code === 'project:import') return permissionState.importAllowed
      if (code === 'project:create') return permissionState.createAllowed
      return false
    },
  }),
}))
vi.mock('vue-router', () => ({ useRouter: () => routerState }))
vi.mock('@/api/project', () => projectApi)
vi.mock('@/api/codeFile', () => ({ uploadFolder: vi.fn() }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn() },
}))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

import ProjectList from './ProjectList.vue'

const ButtonStub = {
  inheritAttrs: false,
  template: '<button v-bind="$attrs"><slot /></button>',
}
const GenericStub = { template: '<div><slot /></div>' }

function mountProjectList() {
  return shallowMount(ProjectList, {
    global: {
      stubs: {
        'el-button': ButtonStub,
        'el-input': GenericStub,
        'el-option': GenericStub,
        'el-select': GenericStub,
        'el-pagination': GenericStub,
        'el-form-item': GenericStub,
        'el-form': GenericStub,
        'el-alert': GenericStub,
        'el-dialog': GenericStub,
        'el-tooltip': GenericStub,
        'el-checkbox': GenericStub,
        ProjectForm: true,
        EmptyState: true,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  permissionState.importAllowed = false
  permissionState.createAllowed = false
  routerState.push.mockReset()
  projectApi.queueRemoteProjectImport.mockReset()
  projectApi.getRemoteProjectImport.mockReset()
  projectApi.cancelRemoteProjectImport.mockReset()
  window.localStorage.clear()
  projectApi.getProjects.mockResolvedValue({ items: [], total: 0 })
})

describe('ProjectList remote import permission', () => {
  it('不向缺少 project:import 权限的只读角色显示远程导入', async () => {
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="remote-import-button"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('向具备 project:import 权限的普通用户显示远程导入', async () => {
    permissionState.importAllowed = true
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="remote-import-button"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ProjectList create permission', () => {
  it('缺少 project:create 时隐藏创建入口且处理器不打开表单', async () => {
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="create-project-button"]').exists()).toBe(false)
    ;(wrapper.vm as any).handleCreate()
    expect((wrapper.vm as any).formVisible).toBe(false)
    wrapper.unmount()
  })

  it('具备 project:create 时显示创建入口', async () => {
    permissionState.createAllowed = true
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="create-project-button"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ProjectList 视图切换', () => {
  it('通过 aria-pressed 暴露当前视图并在点击后同步更新', async () => {
    const wrapper = mountProjectList()
    await flushPromises()

    const [tableButton, cardButton] = wrapper.findAll('.view-btn')
    expect(tableButton.attributes('aria-pressed')).toBe('true')
    expect(cardButton.attributes('aria-pressed')).toBe('false')
    expect(wrapper.find('tbody td[colspan]').attributes('colspan')).toBe('9')

    await cardButton.trigger('click')

    expect(tableButton.attributes('aria-pressed')).toBe('false')
    expect(cardButton.attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('.card-grid').isVisible()).toBe(true)
    expect(wrapper.find('.table-card').isVisible()).toBe(false)
    wrapper.unmount()
  })
})

function remoteTask(overrides: Record<string, unknown> = {}) {
  return {
    task_id: 'remote-task-1',
    status: 'queued',
    attempt_count: 0,
    max_attempts: 3,
    project_id: null,
    result: {},
    error: null,
    next_attempt_at: null,
    started_at: null,
    completed_at: null,
    create_time: '2026-08-27T00:00:00Z',
    update_time: '2026-08-27T00:00:00Z',
    ...overrides,
  }
}

function pendingImport(overrides: Record<string, unknown> = {}) {
  return {
    version: 1,
    taskId: 'remote-task-1',
    idempotencyKey: 'remote-import-test-key',
    payload: {
      url: 'https://example.com/project.zip',
      project_name: '测试项目',
      description: '',
      audit_mode: false,
    },
    ...overrides,
  }
}

describe('ProjectList 远程导入异步任务', () => {
  it('运行中显示取消按钮并防止重复取消请求', async () => {
    permissionState.importAllowed = true
    projectApi.queueRemoteProjectImport.mockResolvedValue(remoteTask({ status: 'downloading' }))
    let resolveCancel!: (value: unknown) => void
    projectApi.cancelRemoteProjectImport.mockReturnValue(new Promise((resolve) => {
      resolveCancel = resolve
    }))

    const wrapper = mountProjectList()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.remoteForm = {
      url: 'https://example.com/project.zip',
      project_name: '可取消项目',
      description: '',
      audit_mode: false,
    }
    await vm.submitRemoteImport()
    await flushPromises()

    expect(wrapper.text()).toContain('正在下载源码')
    const cancelButton = wrapper.find('[data-testid="remote-import-cancel"]')
    expect(cancelButton.exists()).toBe(true)
    await cancelButton.trigger('click')
    await cancelButton.trigger('click')
    expect(projectApi.cancelRemoteProjectImport).toHaveBeenCalledTimes(1)

    resolveCancel(remoteTask({
      status: 'cancelled',
      cancel_reason: '用户在项目页取消远程导入',
      error: { code: 'cancelled', message: '用户在项目页取消远程导入' },
    }))
    await flushPromises()

    expect(wrapper.text()).toContain('远程导入已取消')
    expect(wrapper.text()).toContain('用户在项目页取消远程导入')
    expect(window.localStorage.getItem('prism:remote-import-task')).toBeNull()
    expect(wrapper.find('[data-testid="remote-import-cancel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('提交时携带幂等键，轮询完成后刷新项目并跳转', async () => {
    permissionState.importAllowed = true
    vi.useFakeTimers()
    projectApi.queueRemoteProjectImport.mockResolvedValue(remoteTask())
    projectApi.getRemoteProjectImport
      .mockResolvedValueOnce(remoteTask({ status: 'running', result: { progress: { phase: 'downloading' } } }))
      .mockResolvedValueOnce(remoteTask({
        status: 'succeeded',
        project_id: 42,
        result: { id: 42, file_count: 6 },
      }))

    const wrapper = mountProjectList()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.remoteForm = {
      url: 'https://example.com/project.zip',
      project_name: '测试项目',
      description: '异步导入',
      audit_mode: true,
    }

    await vm.submitRemoteImport()
    await flushPromises()

    expect(projectApi.queueRemoteProjectImport).toHaveBeenCalledWith(
      {
        url: 'https://example.com/project.zip',
        project_name: '测试项目',
        description: '异步导入',
        audit_mode: true,
      },
      expect.stringMatching(/^prism-remote-import-/),
    )
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(projectApi.getRemoteProjectImport).toHaveBeenCalledWith('remote-task-1')
    expect(projectApi.getProjects).toHaveBeenCalledTimes(2)
    expect(routerState.push).toHaveBeenCalledWith('/projects/42')
    expect(window.localStorage.getItem('prism:remote-import-task')).toBeNull()
    wrapper.unmount()
  })

  it('刷新后从 localStorage 恢复任务，网络错误时保留待恢复记录', async () => {
    permissionState.importAllowed = true
    vi.useFakeTimers()
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport()))
    projectApi.getRemoteProjectImport.mockRejectedValue(new Error('网络暂时不可用'))

    const wrapper = mountProjectList()
    await flushPromises()

    expect(projectApi.getRemoteProjectImport).toHaveBeenCalledWith('remote-task-1')
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    expect((wrapper.vm as any).remoteImportError).toContain('网络暂时不可用')

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    wrapper.unmount()
  })

  it('服务端 5xx/429 错误仍保留待恢复记录', async () => {
    permissionState.importAllowed = true
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport()))
    projectApi.getRemoteProjectImport.mockRejectedValue({ code: 50201, message: '上游服务暂不可用' })

    const wrapper = mountProjectList()
    await flushPromises()

    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    expect((wrapper.vm as any).remoteImportError).toBe('上游服务暂不可用')
    wrapper.unmount()
  })

  it('明确的任务不存在错误清理记录并保留失败原因', async () => {
    permissionState.importAllowed = true
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport()))
    projectApi.getRemoteProjectImport.mockRejectedValue({ code: 40400, message: '远程导入任务不存在' })

    const wrapper = mountProjectList()
    await flushPromises()

    expect(window.localStorage.getItem('prism:remote-import-task')).toBeNull()
    expect((wrapper.vm as any).remoteImportError).toBe('远程导入任务不存在')
    wrapper.unmount()
  })

  it('刷新时若提交响应丢失，会用原幂等键重新排队而不是创建重复任务', async () => {
    permissionState.importAllowed = true
    vi.useFakeTimers()
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport({ taskId: null })))
    projectApi.queueRemoteProjectImport.mockResolvedValue(remoteTask())

    const wrapper = mountProjectList()
    await flushPromises()

    expect(projectApi.queueRemoteProjectImport).toHaveBeenCalledWith(
      expect.objectContaining({ project_name: '测试项目' }),
      'remote-import-test-key',
    )
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    wrapper.unmount()
  })

  it('没有导入权限时不恢复或轮询本地任务', async () => {
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport()))

    const wrapper = mountProjectList()
    await flushPromises()

    expect(projectApi.getRemoteProjectImport).not.toHaveBeenCalled()
    expect(projectApi.queueRemoteProjectImport).not.toHaveBeenCalled()
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    wrapper.unmount()
  })

  it('队列请求网络失败时不删除幂等任务，且显示可读提示', async () => {
    permissionState.importAllowed = true
    projectApi.queueRemoteProjectImport.mockRejectedValue(new Error('连接服务器失败'))

    const wrapper = mountProjectList()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.remoteForm = {
      url: 'https://example.com/project.zip',
      project_name: '待恢复项目',
      description: '',
      audit_mode: false,
    }

    await vm.submitRemoteImport()
    await flushPromises()

    const saved = window.localStorage.getItem('prism:remote-import-task')
    expect(saved).toContain('待恢复项目')
    expect(saved).toContain('prism-remote-import-')
    expect((wrapper.vm as any).remoteImportError).toContain('连接服务器失败')
    wrapper.unmount()
  })

  it('任务失败时保留可读原因并清理已结束的恢复记录', async () => {
    permissionState.importAllowed = true
    vi.useFakeTimers()
    projectApi.queueRemoteProjectImport.mockResolvedValue(remoteTask())
    projectApi.getRemoteProjectImport.mockResolvedValue(remoteTask({
      status: 'failed',
      error: { code: 'archive_invalid', message: '压缩包路径校验失败' },
    }))

    const wrapper = mountProjectList()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.remoteForm = {
      url: 'https://example.com/bad.zip',
      project_name: '失败项目',
      description: '',
      audit_mode: false,
    }

    await vm.submitRemoteImport()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect((wrapper.vm as any).remoteImportError).toContain('压缩包路径校验失败')
    expect(wrapper.text()).toContain('压缩包路径校验失败')
    expect(window.localStorage.getItem('prism:remote-import-task')).toBeNull()
    wrapper.unmount()
  })

  it('成功响应缺少项目编号时保留恢复记录并显示可操作原因', async () => {
    permissionState.importAllowed = true
    window.localStorage.setItem('prism:remote-import-task', JSON.stringify(pendingImport()))
    projectApi.getRemoteProjectImport.mockResolvedValue(remoteTask({
      status: 'succeeded',
      project_id: null,
      result: { file_count: 6 },
    }))

    const wrapper = mountProjectList()
    await flushPromises()

    expect((wrapper.vm as any).remoteImportError).toContain('没有返回项目编号')
    expect(window.localStorage.getItem('prism:remote-import-task')).toContain('remote-task-1')
    expect(routerState.push).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
