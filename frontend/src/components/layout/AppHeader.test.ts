import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  permission: true,
  agentPermission: true,
  role: 'user',
  activeSpace: 'personal',
  push: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('@/api/avatar', () => ({ fetchAvatarBlob: async () => null }))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard', matched: [], meta: { title: '工作台' } }),
  useRouter: () => ({
    push: harness.push,
    replace: harness.replace,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: path === '/agents'
        ? { permissions: ['agent:view', 'agent_asset:create'] }
        : path.startsWith('/remote-pentests') ? { permissions: ['pentest:view'] }
          : ['/forum', '/knowledge', '/usage'].includes(path) ? { workspace: 'personal' } : {},
    }),
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    get profile() { return { id: 7, role: harness.role } },
    token: 'token',
    displayName: '普通成员',
    isAdmin: () => harness.role === 'admin' || harness.role === 'super_admin',
    isSuperAdmin: () => harness.role === 'super_admin',
    hasRole: (role: string) => role === harness.role,
    hasPermission: (code: string) => code === 'agent:chat' ? harness.agentPermission : harness.permission,
    logout: vi.fn(),
  }),
}))

vi.mock('@/stores/workspace', () => ({
  getActiveWorkspaceMode: () => harness.activeSpace,
  useWorkspaceStore: () => ({ activeSpace: harness.activeSpace }),
}))

import AppHeader from './AppHeader.vue'

function mountHeader(openAgentChat = vi.fn()) {
  return mount(AppHeader, {
    global: {
      provide: { openAgentChat },
      stubs: {
        'el-icon': true,
        'el-dialog': { template: '<section><slot /></section>' },
        'el-input': true,
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-dropdown-item': { template: '<button><slot /></button>' },
      },
    },
  })
}

describe('AppHeader navigation visibility', () => {
  beforeEach(() => {
    harness.permission = true
    harness.agentPermission = true
    harness.role = 'user'
    harness.activeSpace = 'personal'
    harness.push.mockReset()
    harness.replace.mockReset()
  })

  it('uses the same permission gate for the merged Agent workspace search result', () => {
    harness.role = 'reviewer'
    harness.permission = false
    expect(mountHeader().text()).not.toContain('Agent 工作台')

    harness.permission = true
    expect(mountHeader().text()).toContain('Agent 工作台')
  })

  it('普通用户和审查员使用同一个 Agent 工作台入口', () => {
    harness.role = 'user'
    harness.permission = true
    expect(mountHeader().text()).toContain('Agent 工作台')

    harness.role = 'reviewer'
    expect(mountHeader().text()).toContain('Agent 工作台')
  })

  it('renders accessible top navigation with active underline state', () => {
    const wrapper = mountHeader()
    const dashboard = wrapper.get('.header-nav-item[data-route="/dashboard"]')

    expect(dashboard.classes()).toContain('is-active')
    expect(dashboard.attributes('aria-current')).toBe('page')
    expect(wrapper.get('.agent-trigger').attributes('aria-label')).toBe('打开小菱助手')
    expect(wrapper.get('.agent-trigger .prismling')).toBeTruthy()
    expect(wrapper.find('#roundtable-header-slot').exists()).toBe(false)
    expect(wrapper.find('[aria-label="打开圆桌讨论"]').exists()).toBe(false)
  })

  it('功能导航包含问题追踪、代码沙箱和个人中心', () => {
    const wrapper = mountHeader()
    const items = (wrapper.vm as any).searchItems as Array<{ title: string; path?: string }>
    expect(items).toEqual(expect.arrayContaining([
      expect.objectContaining({ title: '问题追踪', path: '/issues' }),
      expect.objectContaining({ title: '代码沙箱', path: '/sandboxes' }),
      expect.objectContaining({ title: '个人中心', path: '/profile' }),
    ]))
    wrapper.unmount()
  })

  it('企业空间功能导航不显示个人论坛与个人知识库', () => {
    harness.activeSpace = 'enterprise'
    const wrapper = mountHeader()
    const items = (wrapper.vm as any).searchItems as Array<{ path?: string }>
    expect(items.some((item) => item.path === '/forum')).toBe(false)
    expect(items.some((item) => item.path === '/knowledge')).toBe(false)
    wrapper.unmount()
  })

  it('功能导航包含“远程网站评估”，普通用户、审查员和管理员都能搜到', () => {
    for (const role of ['user', 'reviewer', 'admin', 'super_admin']) {
      harness.role = role
      const wrapper = mountHeader()
      const items = (wrapper.vm as any).searchItems as Array<{ title: string; path?: string; description: string }>
      const entry = items.find((item) => item.path === '/remote-pentests')
      expect(entry, role).toBeDefined()
      expect(entry!.title).toBe('远程网站评估')
      expect(entry!.description).toContain('授权')
      wrapper.unmount()
    }
  })

  it('缺少 pentest:view 权限时功能导航不出现远程网站评估', () => {
    harness.role = 'user'
    harness.permission = false
    const wrapper = mountHeader()
    const items = (wrapper.vm as any).searchItems as Array<{ path?: string }>
    expect(items.some((item) => item.path === '/remote-pentests')).toBe(false)
  })

  it('缺少 agent:chat 时不显示小菱按钮和搜索入口', () => {
    harness.agentPermission = false
    const wrapper = mountHeader()

    expect(wrapper.find('.agent-trigger').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('小菱助手')
  })

  it('使用小菱作为顶栏 Agent 助手的主入口名称', () => {
    expect(mountHeader().get('.agent-trigger').text()).toContain('小菱助手')
  })

  it('顶栏小菱按钮可以打开助手面板', async () => {
    const openAgentChat = vi.fn()
    const wrapper = mountHeader(openAgentChat)

    await wrapper.get('.agent-trigger').trigger('click')

    expect(openAgentChat).toHaveBeenCalledOnce()
  })

  it('审查员拥有项目权限时不被静态角色规则误隐藏', () => {
    harness.role = 'reviewer'
    const wrapper = mountHeader()

    expect(wrapper.find('[data-route="/projects"]').exists()).toBe(true)
  })
})
