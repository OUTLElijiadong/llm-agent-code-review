import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  permission: true,
  auditPermission: true,
  role: 'user',
  superAdmin: false,
  authorizePermission: false,
  pending: vi.fn(),
  activeSpace: 'personal',
  activatePersonal: vi.fn(() => { harness.activeSpace = 'personal' }),
  activateEnterprise: vi.fn(() => { harness.activeSpace = 'enterprise' }),
}))

vi.mock('@/api/remotePentest', () => ({ getPendingRemoteApprovalSnapshot: harness.pending }))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard' }),
  useRouter: () => ({
    push: harness.push,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: path === '/agents'
        ? { permissions: ['agent:view', 'agent_asset:create'] }
        : path === '/audit'
          ? { roles: ['reviewer'], permissions: ['audit:view'] }
        : path === '/projects'
          ? { permissions: ['test:permission'] }
          : path.startsWith('/remote-pentests')
            ? { permissions: ['pentest:view'] }
          : ['/forum', '/knowledge', '/usage', '/my-agents'].includes(path) ? { workspace: 'personal' }
            : {},
    }),
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    get profile() { return { id: 7, role: harness.role } },
    token: 'token',
    isAdmin: () => harness.role === 'admin' || harness.role === 'super_admin',
    isSuperAdmin: () => harness.superAdmin,
    hasRole: (role: string) => role === harness.role,
    hasPermission: (code: string) => {
      if (code === 'audit:view') return harness.auditPermission
      if (code === 'pentest:authorize') return harness.authorizePermission
      return harness.permission
    },
  }),
}))

vi.mock('@/stores/workspace', () => ({
  getActiveWorkspaceMode: () => harness.activeSpace,
  useWorkspaceStore: () => ({
    get activeSpace() { return harness.activeSpace },
    get activeEnterpriseId() { return null },
    syncIdentity: vi.fn(),
    activatePersonal: harness.activatePersonal,
    activateEnterprise: harness.activateEnterprise,
  }),
}))

import AppSidebar from './AppSidebar.vue'
import { APP_BUILD_TIME_DISPLAY, APP_DISPLAY_VERSION, APP_RELEASE_SHA, APP_VERSION } from '@/constants/buildInfo'

describe('AppSidebar ordinary member navigation', () => {
  beforeEach(() => {
    window.localStorage.clear()
    harness.push.mockClear()
    harness.permission = true
    harness.auditPermission = true
    harness.role = 'user'
    harness.superAdmin = false
    harness.authorizePermission = false
    harness.activeSpace = 'personal'
    harness.activatePersonal.mockClear()
    harness.activateEnterprise.mockClear()
    harness.pending.mockReset()
    harness.pending.mockResolvedValue({ total: 0, items: [] })
  })

  function mountSidebar(mobileOpen = false) {
    return mount(AppSidebar, {
      props: { mobileOpen },
      global: {
        stubs: {
          'el-icon': true,
          'el-tooltip': { template: '<div><slot /></div>' },
        },
      },
    })
  }

  it('完整显示包含补丁号的三段正式版本', () => {
    expect(APP_DISPLAY_VERSION).toBe(`v${APP_VERSION}`)
    expect(APP_DISPLAY_VERSION).toMatch(/^v\d+\.\d+\.\d+$/)
  })

  it('审查员从统一 Agent 工作台进入 Agent 管理与个人草稿', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/agents"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Agent 工作台')
  })

  it('审查员有 audit:view 时显示操作审计入口，缺权时隐藏', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/audit"]').exists()).toBe(true)

    wrapper.unmount()
    harness.auditPermission = false
    const restricted = mountSidebar()
    expect(restricted.find('[data-route="/audit"]').exists()).toBe(false)
  })

  it('普通用户使用同一 Agent 工作台,不再显示重复的 Agent 工坊入口', () => {
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/agents"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Agent 工作台')
    expect(wrapper.find('[data-route="/agent-studio"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/my-agents"]').exists()).toBe(true)
  })

  it('企业空间不显示个人 Agent 入口', () => {
    harness.activeSpace = 'enterprise'
    const wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/my-agents"]').exists()).toBe(false)
  })

  it('hides Agent Studio when the reviewer lacks its route permission', () => {
    harness.role = 'reviewer'
    harness.permission = false
    const wrapper = mountSidebar()

    expect(wrapper.text()).not.toContain('Agent 工作台')
    expect(wrapper.find('[data-route="/agent-studio"]').exists()).toBe(false)
  })

  it('审查员有项目查看权限时显示项目入口', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/projects"]').exists()).toBe(true)
  })

  it('普通用户与审查员均能从侧栏发现渗透测试入口', () => {
    for (const role of ['user', 'reviewer']) {
      harness.role = role
      const wrapper = mountSidebar()
      expect(wrapper.find('[data-route="/pentests"]').exists()).toBe(true)
      wrapper.unmount()
    }
  })

  it('普通用户、审查员和管理员都能在侧栏找到“远程网站评估”入口', () => {
    for (const role of ['user', 'reviewer', 'admin', 'super_admin']) {
      harness.role = role
      harness.superAdmin = role === 'super_admin'
      const wrapper = mountSidebar()
      const entry = wrapper.find('[data-route="/remote-pentests"]')
      expect(entry.exists(), role).toBe(true)
      expect(entry.text(), role).toContain('远程网站评估')
      wrapper.unmount()
    }
  })

  it('缺少 pentest:view 权限的非管理员看不到远程网站评估入口', () => {
    harness.role = 'user'
    harness.permission = false
    const wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/remote-pentests"]').exists()).toBe(false)
  })

  it('点击入口跳转到远程网站评估列表并关闭移动端抽屉', async () => {
    harness.role = 'admin'
    const wrapper = mountSidebar(true)
    await wrapper.get('[data-route="/remote-pentests"]').trigger('click')
    expect(harness.push).toHaveBeenCalledWith('/remote-pentests')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('企业空间隐藏个人论坛、知识和用量入口，并提供可访问的空间切换按钮', () => {
    harness.activeSpace = 'enterprise'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/knowledge"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/usage"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/enterprise"]').exists()).toBe(true)
    expect(wrapper.get('.workspace-switcher').attributes('aria-label')).toContain('切换到个人空间')
  })

  it('从个人空间切换到企业空间时先更新上下文，再打开企业沟通页', async () => {
    const wrapper = mountSidebar()
    await wrapper.get('.workspace-switcher').trigger('click')

    expect(harness.activateEnterprise).toHaveBeenCalledOnce()
    expect(harness.push).toHaveBeenCalledWith('/enterprise')
  })

  it('拥有 pentest:authorize 的账号看到别人提交的待审批数量角标', async () => {
    harness.role = 'reviewer'
    harness.authorizePermission = true
    harness.pending.mockResolvedValue({ total: 3, items: [{ owner_id: 7 }, { owner_id: 8 }, { owner_id: 9 }] })
    const wrapper = mountSidebar()
    await flushPromises()

    const badge = wrapper.get('[data-route="/remote-pentests"] .nav-badge')
    expect(badge.text()).toBe('2')
    expect(badge.attributes('aria-label')).toBe('2 个远程授权申请待审批')
    expect(harness.pending).toHaveBeenCalledTimes(1)
  })

  it('没有审批权限、没有待审批或接口失败时不显示角标，也不影响入口', async () => {
    harness.role = 'user'
    let wrapper = mountSidebar()
    await flushPromises()
    expect(harness.pending).not.toHaveBeenCalled()
    expect(wrapper.find('.nav-badge').exists()).toBe(false)
    wrapper.unmount()

    harness.role = 'reviewer'
    harness.authorizePermission = true
    wrapper = mountSidebar()
    await flushPromises()
    expect(wrapper.find('.nav-badge').exists()).toBe(false)
    wrapper.unmount()

    harness.pending.mockRejectedValue(new Error('offline'))
    wrapper = mountSidebar()
    await flushPromises()
    expect(wrapper.find('[data-route="/remote-pentests"]').exists()).toBe(true)
    expect(wrapper.find('.nav-badge').exists()).toBe(false)
  })

  it('管理员同样按 pentest:authorize 统计待审批角标', async () => {
    harness.role = 'admin'
    harness.authorizePermission = true
    harness.pending.mockResolvedValue({ total: 1, items: [{ owner_id: 21 }] })
    const wrapper = mountSidebar()
    await flushPromises()

    expect(wrapper.get('[data-route="/remote-pentests"] .nav-badge').text()).toBe('1')
  })

  it('角标超过 99 时显示 99+', async () => {
    harness.role = 'reviewer'
    harness.authorizePermission = true
    harness.pending.mockResolvedValue({
      total: 150,
      items: Array.from({ length: 120 }, (_, index) => ({ owner_id: 100 + index })),
    })
    const wrapper = mountSidebar()
    await flushPromises()

    expect(wrapper.get('[data-route="/remote-pentests"] .nav-badge').text()).toBe('99+')
  })

  it('管理员侧栏保留管理入口并提供论坛与支持中心；安全中心仍限超级管理员', () => {
    harness.role = 'admin'
    let wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/support"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/governance"]').exists()).toBe(true)
    expect(wrapper.findAll('.nav-group-toggle').some((button) => button.text().includes('社区与支持'))).toBe(true)
    expect(wrapper.find('[data-route="/admin/security-center"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/admin/billing-statements"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/admin/billing-reservations"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/admin/billing-pricing"]').exists()).toBe(false)
    expect(wrapper.find('[data-route="/admin/billing-rate-reviews"]').exists()).toBe(true)
    wrapper.unmount()

    harness.role = 'super_admin'
    harness.superAdmin = true
    wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/admin/security-center"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/billing-statements"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/billing-reservations"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/billing-pricing"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows the current release and persists the collapsed island state', async () => {
    const wrapper = mountSidebar()
    expect(wrapper.text()).toContain(`${APP_DISPLAY_VERSION} · PRISM`)
    expect(wrapper.get('.version-meta').text()).toBe(`构建 ${APP_BUILD_TIME_DISPLAY}`)
    expect(wrapper.get('.version').attributes('title')).toContain(APP_RELEASE_SHA)

    await wrapper.get('.sidebar-toggle').trigger('click')

    expect(wrapper.classes()).toContain('is-collapsed')
    expect(window.localStorage.getItem('prism.sidebar.collapsed')).toBe('1')
    expect(wrapper.findAll('.nav-group-toggle').length).toBeGreaterThan(0)
  })

  it('桌面收起状态不妨碍手机抽屉折叠分组与关闭', async () => {
    window.localStorage.setItem('prism.sidebar.collapsed', '1')
    const wrapper = mountSidebar(true)
    const group = wrapper.get('.nav-group-toggle')
    const items = wrapper.get('.nav-group-items')

    expect(items.attributes('style') ?? '').not.toContain('display: none')
    await group.trigger('click')
    expect(group.attributes('aria-expanded')).toBe('false')
    expect(items.attributes('style')).toContain('display: none')

    await wrapper.get('button[aria-label="关闭侧边栏"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
