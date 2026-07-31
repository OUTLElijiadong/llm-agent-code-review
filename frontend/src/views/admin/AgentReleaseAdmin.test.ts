import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentReleaseApproval } from '@/types/agentStudio'

const api = vi.hoisted(() => ({
  approve: vi.fn(),
  disable: vi.fn(),
  listReleases: vi.fn(),
  listApprovals: vi.fn(),
  reject: vi.fn(),
  revise: vi.fn(),
  rollback: vi.fn(),
}))
const element = vi.hoisted(() => ({
  message: { error: vi.fn(), success: vi.fn() },
  box: { prompt: vi.fn(), confirm: vi.fn() },
}))

vi.mock('@/api/agentStudio', () => ({
  approveAgentRelease: api.approve,
  disableCustomAgent: api.disable,
  listAdminAgentReleases: api.listReleases,
  listAgentReleaseApprovals: api.listApprovals,
  rejectAgentRelease: api.reject,
  reviseAgentRelease: api.revise,
  rollbackCustomAgent: api.rollback,
}))
vi.mock('element-plus', () => ({ ElMessage: element.message, ElMessageBox: element.box }))

import AgentReleaseAdmin from './AgentReleaseAdmin.vue'

function release(overrides: Partial<AgentReleaseApproval> = {}): AgentReleaseApproval {
  return {
    id: 7,
    title: '发布代码审查 Agent',
    status: 'pending',
    resource: 'agent:reviewer',
    authoring: {
      prompt: '新版系统提示词',
      review_focus: '重点检查鉴权',
      model_config: { temperature: 0.2, max_tokens: 4096 },
    },
    test_evidence: { passed: true },
    test_evidence_kind: 'static_contract',
    dependencies: [],
    diff: {
      prompt_changed: false,
      review_focus_changed: false,
      model_config_changed: false,
      to_version: 1,
    },
    estimated_calls_per_chunk: 1,
    risk: { level: 'high', requested_capabilities: [] },
    ...overrides,
  }
}

function mountPage(): VueWrapper {
  return mount(AgentReleaseAdmin, {
    global: {
      directives: { loading: () => undefined },
      stubs: {
        'el-button': { props: ['loading', 'disabled'], template: '<button :disabled="disabled"><slot /></button>' },
        'el-segmented': true,
        'el-table': true,
        'el-table-column': true,
        'el-tag': { template: '<span class="tag-stub"><slot /></span>' },
        'el-drawer': { template: '<div class="drawer-stub"><slot /></div>' },
        'el-dialog': { template: '<div class="dialog-stub"><slot /><slot name="footer" /></div>' },
        'el-descriptions': { template: '<div><slot /></div>' },
        'el-descriptions-item': { template: '<div><slot /></div>' },
        'el-form': { template: '<form><slot /></form>' },
        'el-form-item': { template: '<label><slot /></label>' },
        'el-input': true,
        'el-input-number': true,
      },
    },
  })
}

beforeEach(() => {
  api.listApprovals.mockResolvedValue([])
  api.listReleases.mockResolvedValue([])
  api.approve.mockResolvedValue({})
  api.reject.mockResolvedValue({})
})

describe('AgentReleaseAdmin release details', () => {
  it('marks every authoring field as added and shows the complete first release content', async () => {
    const row = release()
    const wrapper = mountPage()
    await flushPromises()
    ;(wrapper.vm as unknown as { openDetail: (value: AgentReleaseApproval) => void }).openDetail(row)
    await flushPromises()

    expect(wrapper.findAll('.release-diff .tag-stub').map((item) => item.text())).toEqual(['新增', '新增', '新增'])
    expect(wrapper.find('.drawer-stub').text()).toContain('首次发布，无前一版本')
    expect(wrapper.find('.drawer-stub').text()).toContain('新版系统提示词')
    expect(wrapper.find('.drawer-stub').text()).toContain('重点检查鉴权')
    expect(wrapper.find('.drawer-stub').text()).toContain('"max_tokens": 4096')
    expect(wrapper.find('.drawer-stub').text()).toContain('静态契约检查证据')
  })

  it('shows actual before and after authoring values for an update', async () => {
    const row = release({
      previous_authoring: {
        prompt: '旧版系统提示词',
        review_focus: '旧审查重点',
        model_config: { temperature: 0.1, max_tokens: 2048 },
      },
      diff: {
        prompt_changed: true,
        review_focus_changed: true,
        model_config_changed: true,
        from_version: 1,
        to_version: 2,
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    ;(wrapper.vm as unknown as { openDetail: (value: AgentReleaseApproval) => void }).openDetail(row)
    await flushPromises()

    const text = wrapper.find('.drawer-stub').text()
    expect(text).toContain('旧版系统提示词')
    expect(text).toContain('新版系统提示词')
    expect(text).toContain('旧审查重点')
    expect(text).toContain('重点检查鉴权')
    expect(text).toContain('"max_tokens": 2048')
    expect(text).toContain('"max_tokens": 4096')
  })

  it('treats prompt cancellation as a no-op and blocks a duplicate decision', async () => {
    const row = release()
    let rejectPrompt = (_reason: unknown): void => undefined
    element.box.prompt.mockImplementation(() => new Promise((_resolve, reject) => { rejectPrompt = reject }))
    const wrapper = mountPage()
    await flushPromises()
    const decide = (wrapper.vm as unknown as {
      decide: (value: AgentReleaseApproval, approve: boolean) => Promise<void>
    }).decide

    const first = decide(row, true)
    const second = decide(row, true)
    rejectPrompt('cancel')
    await Promise.all([first, second])

    expect(element.box.prompt).toHaveBeenCalledTimes(1)
    expect(api.approve).not.toHaveBeenCalled()
    expect(element.message.error).not.toHaveBeenCalled()
  })
})
