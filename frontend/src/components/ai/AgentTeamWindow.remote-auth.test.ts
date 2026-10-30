import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const router = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('vue-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('vue-router')>(),
  useRouter: () => ({
    push: router.push,
    resolve: () => ({ matched: [{}], meta: {} }),
  }),
}))

import type { AgentTeamDetail, AgentTeamTask } from '@/api/agentTeams'
import { useUserStore } from '@/stores/user'
import AgentTeamWindow from './AgentTeamWindow.vue'

function blockedTask(overrides: Record<string, unknown> = {}): AgentTeamTask {
  return {
    task_id: 1,
    task_key: 'probe',
    member_id: 1,
    member_key: 'tester',
    title: '探测站点',
    depends_on: [],
    status: 'blocked',
    priority: 1,
    attempt_count: 1,
    max_attempts: 2,
    input: { operation: 'run_project_tests', project_id: 3, remote_target_url: 'https://example.com/health?x=1' },
    errors: [{ code: 'remote_target_authorization_required', target_origin: 'https://example.com/' }],
    ...overrides,
  } as AgentTeamTask
}

function team(tasks: AgentTeamTask[]): AgentTeamDetail {
  return {
    team_id: 77,
    title: '远程探测团队',
    surface: 'user',
    session_id: 'session-remote',
    status: 'blocked',
    max_active_children: 2,
    trace_id: 'trace-77',
    members: [],
    tasks,
    counts: { total: tasks.length, completed: 0, running: 0, queued: 0, failed: 0, blocked: tasks.length },
    events: [],
    messages: [],
  }
}

function mountWindow(detail: AgentTeamDetail) {
  const pinia = createPinia()
  const wrapper = mount(AgentTeamWindow, {
    props: { visible: true, team: detail },
    global: { stubs: { Teleport: true, Transition: false, 'el-icon': true }, plugins: [pinia] },
  })
  const store = useUserStore(pinia)
  store.token = 'token'
  store.profile = { id: 7, role: 'user' } as never
  store.permissions = new Set(['pentest:view', 'pentest:start']) as never
  return wrapper
}

describe('AgentTeamWindow 远程目标未授权提示', () => {
  beforeEach(() => router.push.mockReset())

  it('远程目标缺少独立授权时在浮窗里出现提示卡，并能跳去申请授权页', async () => {
    const wrapper = mountWindow(team([blockedTask()]))
    await wrapper.vm.$nextTick()

    const notice = wrapper.get('.remote-auth-notice')
    expect(notice.text()).toContain('目标未授权')
    expect(notice.text()).toContain('https://example.com/')
    expect(wrapper.html()).not.toContain('x=1')

    await notice.findAll('button').find((item) => item.text().includes('去申请授权'))!.trigger('click')
    expect(router.push).toHaveBeenCalledWith('/remote-pentests/start?target_url=https%3A%2F%2Fexample.com%2F')
    wrapper.unmount()
  })

  it('监督确认码且任务含远程目标时同样提示；不含远程目标的普通任务不提示', async () => {
    const confirmation = blockedTask({ errors: [{ code: 'supervisor_confirmation_required' }] })
    let wrapper = mountWindow(team([confirmation]))
    expect(wrapper.find('.remote-auth-notice').exists()).toBe(true)
    wrapper.unmount()

    const local = blockedTask({
      errors: [{ code: 'supervisor_confirmation_required' }],
      input: { operation: 'run_project_tests', project_id: 3 },
    })
    wrapper = mountWindow(team([local]))
    expect(wrapper.find('.remote-auth-notice').exists()).toBe(false)
    wrapper.unmount()
  })

  it('没有相关任务的团队不挂载提示卡', () => {
    const ok = blockedTask({ status: 'completed', errors: [] })
    const wrapper = mountWindow(team([ok]))
    expect(wrapper.find('.remote-auth-notice').exists()).toBe(false)
    wrapper.unmount()
  })
})
