import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentReleaseApproval, StudioAsset, StudioVersion } from '@/types/agentStudio'

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

function agentFixture(): StudioAsset {
  return {
    id: 11,
    code: 'agent-reviewer',
    name: '代码审查员',
    owner_id: 2,
    status: 'pending_approval',
    create_time: '2026-09-01 10:00:00',
    update_time: '2026-09-01 10:00:00',
  }
}

function versionFixture(): StudioVersion {
  return {
    id: 21,
    version_number: 3,
    checksum: 'abc',
    status: 'published',
    original_author_id: 2,
    create_time: '2026-09-01 10:00:00',
    update_time: '2026-09-01 10:00:00',
  }
}

function mountPage(): VueWrapper {
  return mount(AgentReleaseAdmin, {
    global: {
      directives: { loading: () => undefined },
      stubs: {
        'el-button': { props: ['loading', 'disabled'], template: '<button :disabled="disabled"><slot /></button>' },
        'el-segmented': true,
        'EmptyState': { props: ['description'], template: '<div class="empty-stub">{{ description }}</div>' },
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

describe('AgentReleaseAdmin approval cards', () => {
  it('renders approvals as cards: agent/version/risk in the main row, metrics on the right, chinese badges', async () => {
    api.listApprovals.mockResolvedValue([
      release({
        agent: agentFixture(),
        version: versionFixture(),
        dependencies: [{ skill: 1 }, { skill: 2 }],
        estimated_calls_per_chunk: 3,
        title: '给审查员补充越权检查能力',
      }),
      release({ id: 8, status: 'approved', agent: agentFixture(), version: versionFixture(), title: '前一次发布' }),
    ])
    const wrapper = mountPage()
    await flushPromises()

    const cards = wrapper.findAll('[data-testid="approval-cards"] .approval-card')
    expect(cards).toHaveLength(2)

    const first = cards[0]
    const mainText = first.find('.rc-line1').text()
    expect(mainText).toContain('#7')
    expect(mainText).toContain('代码审查员')
    expect(mainText).toContain('v3')
    expect(mainText).toContain('高风险')
    expect(mainText).toContain('待审批')
    expect(first.find('.rc-code').text()).toBe('agent-reviewer')
    expect(first.find('[data-testid="approval-metric-skills"]').text()).toContain('2')
    expect(first.find('[data-testid="approval-metric-calls"]').text()).toContain('+3')
    expect(first.text()).toContain('提交说明')
    expect(first.text()).toContain('给审查员补充越权检查能力')
    expect(first.text()).toContain('批准')
    expect(first.text()).toContain('驳回')

    expect(cards[1].find('.rc-line1').text()).toContain('已通过')
    expect(cards[1].text()).not.toContain('批准')
    expect(cards[1].text()).not.toContain('驳回')
  })

  it('keeps the empty state message when there is no approval', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const container = wrapper.find('[data-testid="approval-cards"]')
    expect(container.exists()).toBe(true)
    expect(container.text()).toContain('暂无发布审批')
    expect(wrapper.find('.approval-card').exists()).toBe(false)
  })

  it('opens the detail drawer from the card 查看 button', async () => {
    api.listApprovals.mockResolvedValue([release({ agent: agentFixture(), version: versionFixture() })])
    const wrapper = mountPage()
    await flushPromises()

    const detail = wrapper.findAll('.approval-card button').find((button) => button.text() === '查看')
    expect(detail).toBeTruthy()
    await detail!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.drawer-stub').text()).toContain('代码审查员')
  })

  it('approves from the card through the same decide flow with an opinion prompt', async () => {
    element.box.prompt.mockResolvedValue({ value: '同意发布' })
    api.listApprovals.mockResolvedValue([release({ agent: agentFixture(), version: versionFixture() })])
    const wrapper = mountPage()
    await flushPromises()

    const approve = wrapper.findAll('.approval-card button').find((button) => button.text() === '批准')
    expect(approve).toBeTruthy()
    await approve!.trigger('click')
    await flushPromises()

    expect(element.box.prompt).toHaveBeenCalled()
    expect(api.approve).toHaveBeenCalledWith(7, '同意发布')
  })

  it('expands long submission notes in place', async () => {
    const longNote = '这一版把系统提示词整体重写，并补充了鉴权与越权两类审查重点。'.repeat(3)
    api.listApprovals.mockResolvedValue([release({ agent: agentFixture(), version: versionFixture(), title: longNote })])
    const wrapper = mountPage()
    await flushPromises()

    const note = wrapper.find('.rc-note-text')
    expect(note.text()).toContain(longNote)
    expect(note.classes()).not.toContain('is-expanded')

    const toggle = wrapper.findAll('.rc-note button').find((button) => button.text() === '展开')
    expect(toggle).toBeTruthy()
    await toggle!.trigger('click')

    expect(wrapper.find('.rc-note-text').classes()).toContain('is-expanded')
    expect(wrapper.findAll('.rc-note button').some((button) => button.text() === '收起')).toBe(true)
  })
})
