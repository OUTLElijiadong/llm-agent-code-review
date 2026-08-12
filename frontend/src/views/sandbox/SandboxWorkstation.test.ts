import { flushPromises, shallowMount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listSandboxes: vi.fn(), getSandbox: vi.fn(), createSandbox: vi.fn(), stopSandbox: vi.fn(),
  extendSandbox: vi.fn(), createSandboxPreviewSession: vi.fn(), searchSandboxCapabilities: vi.fn(),
}))
const projectApi = vi.hoisted(() => ({ getProjects: vi.fn() }))
const environment = {
  public_id: 'sbx_1', project_id: 7, owner_id: 2, worker_code: 'managed-1', agent_code: 'test_verifier',
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
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isSuperAdmin: () => false }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: vi.fn() } }))

import SandboxWorkstation from './SandboxWorkstation.vue'

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

beforeEach(() => {
  vi.clearAllMocks()
  projectApi.getProjects.mockResolvedValue({
    items: [{ id: 7, project_name: '项目 A', status: 'active', file_count: 1, create_time: '' }],
    total: 1,
  })
  api.listSandboxes.mockResolvedValue([environment])
  api.getSandbox.mockResolvedValue(environment)
  api.searchSandboxCapabilities.mockResolvedValue([])
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
        { id: 7, project_name: 'Python 项目', language: 'python', status: 'active', file_count: 1, create_time: '' },
        { id: 8, project_name: 'PHP 项目', language: 'php', status: 'active', file_count: 1, create_time: '' },
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
        { id: 7, project_name: 'Python 项目', language: 'python', status: 'active', file_count: 1, create_time: '' },
        { id: 8, project_name: 'PHP 项目', language: 'PHP 8.3', status: 'active', file_count: 1, create_time: '' },
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
      items: [{ id: 9, project_name: '未知语言项目', language: 'plaintext', status: 'active', file_count: 1, create_time: '' }],
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
})
