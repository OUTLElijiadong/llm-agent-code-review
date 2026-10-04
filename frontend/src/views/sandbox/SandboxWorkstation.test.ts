import { flushPromises, shallowMount } from '@vue/test-utils'
import { nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listSandboxes: vi.fn(), getSandbox: vi.fn(), createSandbox: vi.fn(), authorizeSandboxRemoteTarget: vi.fn(), stopSandbox: vi.fn(),
  extendSandbox: vi.fn(), downloadSandboxArtifact: vi.fn(), createSandboxPreviewSession: vi.fn(), searchSandboxCapabilities: vi.fn(),
}))
const projectApi = vi.hoisted(() => ({ getProjects: vi.fn(), getProjectDetail: vi.fn() }))
const confirmation = vi.hoisted(() => vi.fn())
const environment = {
  public_id: 'sbx_1', project_id: 7, owner_id: 2, can_execute: true, can_preview: true, can_stop: true, worker_code: 'managed-1', agent_code: 'test_verifier',
  purpose: 'test', language: 'python', test_mode: 'combined', status: 'succeeded', runtime: 'runsc',
  source_sha256: 'a'.repeat(64), expires_at: '2026-08-05T00:00:00', result: { summary: '测试通过' },
  events: [
    { id: 2, event_type: 'complete', stage: 'conclusion', message: '测试通过', payload: {}, create_time: '2026-08-02T10:00:02' },
    { id: 1, event_type: 'dispatch', stage: 'worker', message: '已调用 worker', payload: {}, create_time: '2026-08-02T10:00:01' },
  ],
}

vi.mock('@/api/sandbox', () => api)
vi.mock('@/api/project', () => projectApi)
vi.mock('@/api/mcpGovernance', () => ({ listSandboxWorkers: vi.fn().mockResolvedValue([]) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => user }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: confirmation } }))

import SandboxWorkstation from './SandboxWorkstation.vue'

const user = reactive({ profile: { id: 11 }, token: 'account-a', isSuperAdmin: () => false, hasPermission: (code: string) => ['project:view', 'file:view'].includes(code) })

const mountOptions = {
  global: {
    stubs: {
      'el-alert': { props: ['title', 'type'], template: '<div :data-type="type">{{ title }}<slot /></div>' },
      'el-button': { template: '<button><slot /></button>' },
      'el-checkbox': { template: '<label><slot /></label>' },
      'el-empty': { template: '<div />' },
      'el-form': { template: '<form><slot /></form>' },
      'el-form-item': { template: '<div><slot /></div>' },
      'el-icon': { template: '<i><slot /></i>' },
      'el-input': { template: '<input />' },
      'el-input-number': { template: '<input />' },
      'el-option': true,
      'el-radio-button': { template: '<span><slot /></span>' },
      'el-radio-group': { template: '<div><slot /></div>' },
      'el-select': { template: '<div><slot /></div>' },
      'el-tag': { template: '<span><slot /></span>' },
    },
    directives: { loading: {} },
  },
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.values(api).forEach((mock) => mock.mockReset())
  Object.values(projectApi).forEach((mock) => mock.mockReset())
  confirmation.mockReset()
  user.profile = { id: 11 }
  user.token = 'account-a'
  confirmation.mockResolvedValue(true)
  projectApi.getProjects.mockResolvedValue({
    items: [{ id: 7, project_name: '项目 A', status: 'active', can_execute: true, file_count: 1, create_time: '' }],
    total: 1,
  })
  projectApi.getProjectDetail.mockResolvedValue({ source_revisions: [] })
  api.listSandboxes.mockResolvedValue([environment])
  api.getSandbox.mockResolvedValue(environment)
  api.searchSandboxCapabilities.mockResolvedValue([])
  api.authorizeSandboxRemoteTarget.mockResolvedValue({ approval_token: '31.secret', expires_at: '2026-09-28T12:05:00' })
  api.createSandbox.mockResolvedValue({ ...environment, public_id: 'sbx_created' })
  api.stopSandbox.mockResolvedValue({ ...environment, status: 'stopped' })
  api.extendSandbox.mockResolvedValue({ ...environment, status: 'ready' })
  api.downloadSandboxArtifact.mockResolvedValue(new Blob(['sandbox-evidence']))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SandboxWorkstation Agent output ordering', () => {
  it('renders the complete Agent call timeline before the conclusion', async () => {
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()

    const timeline = wrapper.get('[data-testid="agent-timeline"]')
    const conclusion = wrapper.get('[data-testid="agent-conclusion"]')
    expect(timeline.text()).toContain('已调用 worker')
    expect(timeline.text().indexOf('已调用 worker')).toBeLessThan(timeline.text().indexOf('测试通过'))
    expect(timeline.element.compareDocumentPosition(conclusion.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    wrapper.unmount()
  })

  it('synchronizes the deployment runtime whenever the selected project changes', async () => {
    projectApi.getProjects.mockResolvedValue({
      items: [
        { id: 7, project_name: 'Python 项目', language: 'python', status: 'active', can_execute: true, file_count: 1, create_time: '' },
        { id: 8, project_name: 'PHP 项目', language: 'php', status: 'active', can_execute: true, file_count: 1, create_time: '' },
      ],
      total: 2,
    })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { project_id: number | null; purpose: string; language: string }
    }

    expect(vm.form.language).toBe('python')
    vm.form.project_id = 8
    await nextTick()
    expect(vm.form.language).toBe('php')

    vm.form.language = 'python'
    vm.form.purpose = 'deploy'
    await nextTick()
    expect(vm.form.language).toBe('php')
    wrapper.unmount()
  })

  it('recomputes the project runtime at submit and sends an exact PHP deployment payload', async () => {
    projectApi.getProjects.mockResolvedValue({
      items: [
        { id: 7, project_name: 'Python 项目', language: 'python', status: 'active', can_execute: true, file_count: 1, create_time: '' },
        { id: 8, project_name: 'PHP 项目', language: 'PHP 8.3', status: 'active', can_execute: true, file_count: 1, create_time: '' },
      ],
      total: 2,
    })
    api.createSandbox.mockResolvedValue({
      ...environment,
      public_id: 'sbx_deploy_php',
      project_id: 8,
      purpose: 'deploy',
      language: 'php',
      test_mode: 'deploy',
      status: 'ready',
    })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { project_id: number | null; purpose: string; language: string }
      submit: () => Promise<void>
    }

    vm.form.project_id = 8
    vm.form.purpose = 'deploy'
    await nextTick()
    vm.form.language = 'python'
    await vm.submit()

    expect(api.createSandbox).toHaveBeenCalledOnce()
    expect(api.createSandbox).toHaveBeenCalledWith({
      project_id: 8,
      purpose: 'deploy',
      language: 'php',
      test_mode: 'deploy',
      worker_code: undefined,
      ttl_hours: 72,
      remote_target_url: undefined,
      remote_target_authorized: false,
    })
    wrapper.unmount()
  })

  it('blocks deployment when the project language has no controlled runtime', async () => {
    projectApi.getProjects.mockResolvedValue({
      items: [{ id: 9, project_name: '未知语言项目', language: 'plaintext', status: 'active', can_execute: true, file_count: 1, create_time: '' }],
      total: 1,
    })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { project_id: number | null; purpose: string }
      submitDisabled: boolean
      submit: () => Promise<void>
    }

    vm.form.purpose = 'deploy'
    await nextTick()
    expect(vm.submitDisabled).toBe(true)
    await vm.submit()
    expect(api.createSandbox).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('issues a target and mode bound authorization before creating a remote blackbox run', async () => {
    api.createSandbox.mockResolvedValue({ ...environment, public_id: 'sbx_remote_1' })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: {
        project_id: number | null
        purpose: string
        language: string
        test_mode: string
        remote_target_url: string
        remote_target_authorized: boolean
      }
      submit: () => Promise<void>
    }
    vm.form.project_id = 7
    vm.form.purpose = 'test'
    vm.form.language = 'python'
    vm.form.test_mode = 'blackbox'
    vm.form.remote_target_url = 'https://authorized.example/path'
    await nextTick()
    vm.form.remote_target_authorized = true

    await vm.submit()

    expect(api.authorizeSandboxRemoteTarget).toHaveBeenCalledOnce()
    expect(api.authorizeSandboxRemoteTarget).toHaveBeenCalledWith({
      project_id: 7,
      remote_target_url: 'https://authorized.example/path',
      test_mode: 'blackbox',
      confirmed: true,
    })
    expect(api.createSandbox).toHaveBeenCalledWith(expect.objectContaining({
      remote_target_url: 'https://authorized.example/path',
      remote_target_approval_token: '31.secret',
    }))
    expect(api.authorizeSandboxRemoteTarget.mock.invocationCallOrder[0])
      .toBeLessThan(api.createSandbox.mock.invocationCallOrder[0])
    wrapper.unmount()
  })

  it('shows a finalizing result as pending report generation instead of a failed conclusion', async () => {
    const finalizing = {
      ...environment,
      status: 'finalizing',
      result: { passed: true, summary: '白盒和黑盒测试已通过' },
    }
    api.listSandboxes.mockResolvedValue([finalizing])
    api.getSandbox.mockResolvedValue(finalizing)
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()

    const conclusion = wrapper.get('[data-testid="agent-conclusion"]')
    expect(conclusion.text()).toContain('确定性结果已生成，审查报告生成中')
    expect(conclusion.find('[data-type]').attributes('data-type')).toBe('warning')
    expect(wrapper.text()).toContain('自动刷新')
    wrapper.unmount()
  })

  it('does not offer renewal while the sandbox is closing', async () => {
    const stopping = { ...environment, status: 'stopping', result: {} }
    api.listSandboxes.mockResolvedValue([stopping])
    api.getSandbox.mockResolvedValue(stopping)
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()

    expect(wrapper.text()).not.toContain('续期 24h')
    expect(wrapper.findAll('button').some((button) => button.text() === '关闭')).toBe(false)
    wrapper.unmount()
  })

  it('keeps polling a finalizing sandbox after the 2.5 second interval', async () => {
    vi.useFakeTimers()
    const finalizing = { ...environment, status: 'finalizing', result: { passed: true } }
    api.listSandboxes.mockResolvedValue([finalizing])
    api.getSandbox.mockResolvedValue(finalizing)
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    try {
      await flushPromises()
      expect(api.listSandboxes).toHaveBeenCalledTimes(1)
      expect(api.getSandbox).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(2500)
      await flushPromises()

      expect(api.listSandboxes).toHaveBeenCalledTimes(2)
      expect(api.getSandbox).toHaveBeenCalledWith('sbx_1')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('项目快速切换后只显示当前项目的修复副本', async () => {
    projectApi.getProjects.mockResolvedValue({
      items: [
        { id: 7, project_name: '项目 A', language: 'python', status: 'active', can_execute: true, file_count: 1, create_time: '' },
        { id: 8, project_name: '项目 B', language: 'node', status: 'active', can_execute: true, file_count: 1, create_time: '' },
      ],
      total: 2,
    })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    const revisionA = deferred<any>()
    const revisionB = deferred<any>()
    projectApi.getProjectDetail.mockImplementation((projectId: number) => (
      projectId === 7 ? revisionA.promise : revisionB.promise
    ))
    vm.form.project_id = null
    await nextTick()
    vm.form.project_id = 7
    await nextTick()
    vm.form.project_id = 8
    await nextTick()
    revisionB.resolve({ source_revisions: [{ id: 202, revision_no: 2, repaired_files: [] }] })
    await flushPromises()
    revisionA.resolve({ source_revisions: [{ id: 101, revision_no: 1, repaired_files: [] }] })
    await flushPromises()

    expect(vm.form.project_id).toBe(8)
    expect(vm.sourceRevisions.map((revision: { id: number }) => revision.id)).toEqual([202])
    wrapper.unmount()
  })

  it('初始请求在卸载后完成时不注册轮询或重复读取沙箱', async () => {
    vi.useFakeTimers()
    const projects = deferred<any>()
    projectApi.getProjects.mockReturnValue(projects.promise)
    api.listSandboxes.mockResolvedValue([{ ...environment, status: 'running' }])
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await nextTick()
    wrapper.unmount()
    projects.resolve({ items: [{ id: 7, project_name: '项目 A', status: 'active', can_execute: true, language: 'python' }], total: 1 })
    await flushPromises()
    const callsAfterUnmount = api.listSandboxes.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()

    expect(api.listSandboxes).toHaveBeenCalledTimes(callsAfterUnmount)
  })

  it('关闭确认期间切换沙箱时取消操作，不能关闭另一任务', async () => {
    api.listSandboxes.mockResolvedValue([{ ...environment, status: 'ready' }, { ...environment, public_id: 'sbx_2', status: 'ready' }])
    api.stopSandbox.mockResolvedValueOnce({ ...environment, public_id: 'sbx_2', status: 'stopped' })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    const pending = deferred<boolean>()
    confirmation.mockReturnValueOnce(pending.promise)
    const request = vm.stopCurrent()
    vm.selectedId = 'sbx_2'
    pending.resolve(true)
    await request
    expect(api.stopSandbox).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('远程目标授权等待期间配置变化时不使用旧授权创建新任务', async () => {
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    vm.form.test_mode = 'blackbox'
    vm.form.remote_target_url = 'https://authorized.example/'
    await nextTick()
    vm.form.remote_target_authorized = true
    const approval = deferred<any>()
    api.authorizeSandboxRemoteTarget.mockReturnValueOnce(approval.promise)
    api.createSandbox.mockResolvedValue({ ...environment, public_id: 'sbx_new' })
    const request = vm.submit()
    vm.form.project_id = 8
    vm.form.remote_target_url = 'https://changed.example/'
    await nextTick()
    approval.resolve({ approval_token: 'old-approved-target' })
    await request
    expect(api.createSandbox).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('重复提交在前一任务等待授权期间仅发起一次授权', async () => {
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    vm.form.test_mode = 'blackbox'
    vm.form.remote_target_url = 'https://authorized.example/'
    await nextTick()
    vm.form.remote_target_authorized = true
    const approval = deferred<any>()
    api.authorizeSandboxRemoteTarget.mockReturnValue(approval.promise)
    api.createSandbox.mockResolvedValue({ ...environment, public_id: 'sbx_new' })
    const request = vm.submit()
    const duplicate = vm.submit()
    expect(api.authorizeSandboxRemoteTarget).toHaveBeenCalledTimes(1)
    approval.resolve({ approval_token: 'approved' })
    await Promise.all([request, duplicate])
    expect(api.createSandbox).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('账号切换立即清空旧沙箱，迟到的刷新不能恢复旧数据', async () => {
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    const stale = deferred<any>()
    api.listSandboxes.mockReturnValueOnce(stale.promise).mockResolvedValue([])
    projectApi.getProjects.mockResolvedValue({ items: [], total: 0 })
    const request = vm.refreshSelected(true)
    user.profile = { id: 12 }
    user.token = 'account-b'
    await flushPromises()
    expect(vm.environments).toEqual([])
    stale.resolve([environment])
    await request
    expect(vm.environments).toEqual([])
    expect(vm.selectedId).toBe('')
    wrapper.unmount()
  })

  it.each(['javascript:alert(1)', 'https://untrusted.example/api/sandboxes/sbx_1/preview/'])('预览拒绝不受控路径 %s', async (path) => {
    api.listSandboxes.mockResolvedValue([{ ...environment, status: 'ready', preview_path: '/api/sandboxes/sbx_1/preview/' }])
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(previewWindow as any)
    api.createSandboxPreviewSession.mockResolvedValueOnce({ path })
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    await (wrapper.vm as any).openPreview()
    expect(previewWindow.location.replace).not.toHaveBeenCalled()
    expect(previewWindow.close).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('项目读取失败仍显示已加载的沙箱任务，并提供重新加载恢复', async () => {
    const errors = vi.fn()
    projectApi.getProjects.mockRejectedValueOnce(new Error('项目服务暂时不可用'))
    const wrapper = shallowMount(SandboxWorkstation, { ...mountOptions, global: { ...mountOptions.global, config: { errorHandler: errors } } })
    await flushPromises()
    expect(wrapper.get('[data-testid="agent-timeline"]').text()).toContain('已调用 worker')
    expect(wrapper.get('[data-testid="sandbox-load-error"]').text()).toContain('项目服务暂时不可用')
    expect(errors).not.toHaveBeenCalled()
    await (wrapper.vm as any).loadInitial()
    await flushPromises()
    expect(wrapper.find('[data-testid="sandbox-load-error"]').exists()).toBe(false)
    expect((wrapper.vm as any).projects).toHaveLength(1)
    wrapper.unmount()
  })

  it('沙箱读取失败保留成功的项目选项，重试后恢复任务且清除局部错误', async () => {
    const errors = vi.fn()
    api.listSandboxes.mockRejectedValueOnce(new Error('任务服务暂时不可用'))
    const wrapper = shallowMount(SandboxWorkstation, { ...mountOptions, global: { ...mountOptions.global, config: { errorHandler: errors } } })
    await flushPromises()
    expect((wrapper.vm as any).projects).toHaveLength(1)
    expect(wrapper.get('[data-testid="sandbox-load-error"]').text()).toContain('任务服务暂时不可用')
    expect(errors).not.toHaveBeenCalled()
    await (wrapper.vm as any).loadInitial()
    await flushPromises()
    expect(wrapper.find('[data-testid="sandbox-load-error"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="agent-timeline"]').text()).toContain('已调用 worker')
    wrapper.unmount()
  })

  it('安全的同源当前沙箱路径可以继续打开预览', async () => {
    const path = '/api/sandboxes/sbx_1/preview/'
    api.listSandboxes.mockResolvedValue([{ ...environment, status: 'ready', preview_path: path }])
    api.createSandboxPreviewSession.mockResolvedValueOnce({ path })
    const previewWindow = { opener: {}, location: { replace: vi.fn() }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(previewWindow as any)
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    await (wrapper.vm as any).openPreview()
    expect(previewWindow.location.replace).toHaveBeenCalledExactlyOnceWith(`${window.location.origin}${path}`)
    expect(previewWindow.close).not.toHaveBeenCalled()
    expect(previewWindow.opener).toBeNull()
    wrapper.unmount()
  })

  it.each([
    ['创建任务', 'submit', 'createSandbox', 'submitting'],
    ['远程授权', 'submit', 'authorizeSandboxRemoteTarget', 'submitting'],
    ['关闭任务', 'stopCurrent', 'stopSandbox', 'mutating'],
    ['延长任务', 'extendCurrent', 'extendSandbox', 'mutating'],
    ['下载证据', 'downloadArtifact', 'downloadSandboxArtifact', 'mutating'],
    ['检索能力', 'searchCapabilities', 'searchSandboxCapabilities', 'capabilitiesLoading'],
  ] as const)('%s接口失败不会泄漏未处理异常，原场景重试可恢复', async (_label, method, apiMethod, busyField) => {
    api.listSandboxes.mockResolvedValue([{ ...environment, status: 'ready' }])
    URL.createObjectURL = vi.fn(() => 'blob:sandbox-evidence')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = shallowMount(SandboxWorkstation, mountOptions)
    await flushPromises()
    const vm = wrapper.vm as any
    if (apiMethod === 'authorizeSandboxRemoteTarget') {
      vm.form.test_mode = 'blackbox'
      vm.form.remote_target_url = 'https://authorized.example/'
      await nextTick()
      vm.form.remote_target_authorized = true
    }
    const args = method === 'downloadArtifact' ? [{ id: 4, artifact_type: 'evidence', file_name: 'evidence.txt' }] : []
    api[apiMethod].mockRejectedValueOnce(new Error('临时网络失败'))
    await expect(vm[method](...args)).resolves.toBeUndefined()
    expect(vm[busyField]).toBe(false)
    await expect(vm[method](...args)).resolves.toBeUndefined()
    expect(api[apiMethod]).toHaveBeenCalledTimes(2)
    expect(vm[busyField]).toBe(false)
    wrapper.unmount()
  })

})
