import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  canStart: true,
  canView: true,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push: harness.push,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: path === '/remote-pentests/start'
        ? { permissions: ['pentest:start'] }
        : { permissions: ['pentest:view'] },
    }),
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    token: 'token',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: () => true,
    hasPermission: (code: string) => (
      code === 'pentest:start' ? harness.canStart : code === 'pentest:view' ? harness.canView : false
    ),
  }),
}))

import type { AgentTeamTask } from '@/api/agentTeams'
import AgentTeamRemoteAuthNotice from './AgentTeamRemoteAuthNotice.vue'

function task(overrides: Partial<AgentTeamTask> & Record<string, unknown> = {}): AgentTeamTask {
  return {
    task_id: 1,
    task_key: 'probe',
    member_id: 1,
    title: '探测站点',
    depends_on: [],
    status: 'blocked',
    priority: 1,
    attempt_count: 1,
    max_attempts: 2,
    input: { operation: 'run_project_tests', project_id: 3, remote_target_url: 'https://example.com/health' },
    errors: [{ code: 'remote_target_authorization_required', target_origin: 'https://example.com/' }],
    ...overrides,
  } as AgentTeamTask
}

function render(tasks: AgentTeamTask[]) {
  return mount(AgentTeamRemoteAuthNotice, { props: { tasks } })
}

function button(wrapper: ReturnType<typeof render>, label: string) {
  return wrapper.findAll('button').find((item) => item.text().includes(label))
}

describe('AgentTeamRemoteAuthNotice 目标未授权提示卡', () => {
  beforeEach(() => {
    harness.push.mockReset()
    harness.canStart = true
    harness.canView = true
  })

  it('远程目标缺少独立授权时显示提示卡，并跳到带目标预填的申请页', async () => {
    const wrapper = render([task()])

    expect(wrapper.text()).toContain('目标未授权')
    expect(wrapper.text()).toContain('该远程目标尚未获得独立授权，请先提交远程网站评估并由另一位审批人批准')
    expect(wrapper.text()).toContain('https://example.com/')
    expect(wrapper.text()).toContain('探测站点')

    await button(wrapper, '去申请授权')!.trigger('click')
    expect(harness.push).toHaveBeenCalledWith('/remote-pentests/start?target_url=https%3A%2F%2Fexample.com%2F')

    await button(wrapper, '查看已有授权')!.trigger('click')
    expect(harness.push).toHaveBeenLastCalledWith('/remote-pentests')
  })

  it('监督确认码只有在任务含远程目标时才提示，且只展示目标来源不带路径和查询', () => {
    const confirmation = task({
      errors: [{ code: 'supervisor_confirmation_required', risk_level: 'high' }],
      input: { operation: 'run_project_tests', remote_target_url: 'https://user:pw@Example.com/health?token=abc#frag' },
    })
    const wrapper = render([confirmation])

    expect(wrapper.text()).toContain('目标未授权')
    expect(wrapper.text()).toContain('https://example.com/')
    expect(wrapper.html()).not.toContain('token=abc')
    expect(wrapper.html()).not.toContain('user:pw')
    expect(wrapper.html()).not.toContain('/health')

    const local = task({
      errors: [{ code: 'supervisor_confirmation_required', risk_level: 'high' }],
      input: { operation: 'run_project_tests', project_id: 3 },
    })
    expect(render([local]).find('.remote-auth-notice').exists()).toBe(false)
  })

  it('目标来源优先取后端给出的脱敏 next_action，其次才读任务输入', () => {
    const fromNextAction = task({
      errors: [{ code: 'remote_target_authorization_required' }],
      result: { next_action: { open_path: '/remote-pentests/start', target_url: 'https://from-next-action.example.org/' } },
      input: {},
    })
    expect(render([fromNextAction]).text()).toContain('https://from-next-action.example.org/')
  })

  it('已完成、运行中的任务和没有相关错误码的任务不显示提示卡', () => {
    const completed = task({ status: 'completed' })
    const running = task({ status: 'running' })
    const unrelated = task({ errors: [{ code: 'execution_failed', message: 'SECRET_ERROR' }] })

    for (const item of [completed, running, unrelated]) {
      const wrapper = render([item])
      expect(wrapper.find('.remote-auth-notice').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('SECRET_ERROR')
    }
  })

  it('同一目标的多个任务合并成一张卡并列出任务名', () => {
    const wrapper = render([
      task({ task_id: 1, task_key: 'a', title: '探测首页' }),
      task({ task_id: 2, task_key: 'b', title: '复测响应头' }),
      task({
        task_id: 3,
        task_key: 'c',
        title: '另一站点',
        errors: [{ code: 'remote_target_authorization_required', target_origin: 'https://other.example.org/' }],
      }),
    ])

    const cards = wrapper.findAll('.remote-auth-notice')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toContain('探测首页')
    expect(cards[0].text()).toContain('复测响应头')
    expect(cards[1].text()).toContain('https://other.example.org/')
  })

  it('远程评估无法授权的目标（非 HTTPS 默认端口或 IP）不给申请按钮并说明原因', () => {
    for (const origin of ['https://example.com:8443/', 'http://example.com/', 'https://203.0.113.9/']) {
      const wrapper = render([task({ errors: [{ code: 'remote_target_authorization_required', target_origin: origin }] })])
      expect(button(wrapper, '去申请授权')).toBeUndefined()
      expect(wrapper.text()).toContain('仅支持 HTTPS 默认端口的域名站点根地址')
      expect(button(wrapper, '查看已有授权')).toBeDefined()
    }
  })

  it('按账号权限显示按钮；两项权限都没有时提示联系管理员', () => {
    harness.canStart = false
    let wrapper = render([task()])
    expect(button(wrapper, '去申请授权')).toBeUndefined()
    expect(button(wrapper, '查看已有授权')).toBeDefined()

    harness.canStart = true
    harness.canView = false
    wrapper = render([task()])
    expect(button(wrapper, '去申请授权')).toBeDefined()
    expect(button(wrapper, '查看已有授权')).toBeUndefined()

    harness.canStart = false
    wrapper = render([task()])
    expect(wrapper.text()).toContain('当前账号没有远程网站评估权限')
  })

  it('不显示错误详情里的内部原因文本', () => {
    const wrapper = render([task({
      errors: [
        { code: 'remote_target_authorization_required', target_origin: 'https://example.com/', message: 'SECRET_ERROR' },
        { code: 'execution_failed', message: 'SECRET_EXECUTION_FAILED' },
      ],
    })])

    expect(wrapper.text()).not.toContain('SECRET_ERROR')
    expect(wrapper.text()).not.toContain('SECRET_EXECUTION_FAILED')
  })
})
