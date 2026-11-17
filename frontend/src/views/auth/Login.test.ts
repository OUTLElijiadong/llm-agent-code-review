import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createRequire } from 'node:module'
import { reactive, type Plugin } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const user = vi.hoisted(() => ({ login: vi.fn(), token: '', profile: { role: 'user' } }))
const messages = vi.hoisted(() => ({ success: vi.fn() }))
const ElementPlus = (createRequire(import.meta.url)('element-plus') as { default: Plugin }).default

const reactiveUser = reactive(user)
vi.mock('@/stores/user', () => ({ useUserStore: () => reactiveUser }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import Login from './Login.vue'

let wrapper: VueWrapper

async function renderLogin(initialPath = '/login') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['/login', '/register', '/dashboard', '/admin/operations', '/admin/platform', '/billing', '/projects/:id', '/403'].map((path) => ({
      path,
      component: { template: '<div />' },
    })),
  })
  await router.push(initialPath)
  await router.isReady()
  wrapper = mount(Login, { attachTo: document.body, global: { plugins: [ElementPlus, router] } })
  return router
}

async function fillCredentials() {
  await wrapper.get('input[autocomplete="username"]').setValue('reviewer')
  await wrapper.get('input[autocomplete="current-password"]').setValue('test-password')
}

beforeEach(() => {
  vi.stubEnv('VITE_APP_VERSION', '')
  user.profile.role = 'user'
  reactiveUser.token = ''
  user.login.mockImplementation(async () => { reactiveUser.token = 'test-session' })
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
  vi.unstubAllEnvs()
})

describe('登录真实反馈', () => {
  it('入口只保留品牌、登录表单和注册路径', async () => {
    await renderLogin()
    expect(wrapper.text()).toContain('Prism · 棱镜')
    expect(wrapper.get('h1').text()).toBe('登录')
    expect(wrapper.text()).not.toMatch(/AI CODE REVIEW|折射真相|代码审查 · 问题跟踪|构建版本|© 2026/)
    expect(wrapper.text()).toContain('没有账号？')
    expect(wrapper.get('a.link').attributes('href')).toBe('/register')
  })

  it('无论构建注入与否都不在登录表单显示版本脚注', async () => {
    vi.stubEnv('VITE_APP_VERSION', '4.1.46')
    await renderLogin()
    expect(wrapper.text()).not.toContain('4.1.46')
    expect(wrapper.text()).not.toContain('构建版本')
  })

  it('真实表单校验阻止空提交并展示必填错误', async () => {
    await renderLogin()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(wrapper.findAll('.el-form-item__error').map((item) => item.text())).toEqual([
      '请输入账号', '请输入密码',
    ]))
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
  })

  it.each(['user', 'super_admin'])('成功登录后保留 %s 的角色首页规则', async (role) => {
    user.profile.role = role
    const router = await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledExactlyOnceWith({ username: 'reviewer', password: 'test-password' })
    expect(router.currentRoute.value.path).toBe(role === 'user' ? '/dashboard' : '/admin/operations')
    expect(wrapper.get('[role="status"]').text()).toContain('登录成功')
  })

  it.each([
    ['user', '/projects/7?tab=history', '/projects/7?tab=history'],
    ['super_admin', '/billing?tab=orders', '/billing?tab=orders'],
    ['super_admin', '/admin/platform?section=llm', '/admin/platform?section=llm'],
    ['user', '//example.invalid/outside', '/dashboard'],
    ['user', '/\\\\example.invalid/outside', '/dashboard'],
    ['user', '/projects/7\n?tab=history', '/dashboard'],
    ['user', 'https://example.invalid/outside', '/dashboard'],
    ['user', '/not-an-existing-route', '/dashboard'],
    ['user', '/login?redirect=/projects/7', '/dashboard'],
  ])('登录后恢复安全站内目标 %s %s', async (role, redirect, expected) => {
    user.profile.role = role
    const router = await renderLogin(`/login?redirect=${encodeURIComponent(redirect)}`)
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(expected)
  })

  it('恢复目标时仍执行路由权限守卫', async () => {
    const router = await renderLogin('/login?redirect=%2Fadmin%2Fplatform')
    router.beforeEach((to) => to.path === '/admin/platform' ? '/403' : true)
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/403')
  })

  it('登录成功后目标导航抛错时回到首页，不重复登录', async () => {
    const router = await renderLogin('/login?redirect=%2Fbilling')
    const replace = vi.spyOn(router, 'replace')
    replace.mockRejectedValueOnce(new Error('navigation failed'))
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe('/dashboard')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('目标导航被守卫取消时允许只重试进入，无需再次登录', async () => {
    const router = await renderLogin('/login?redirect=%2Fbilling')
    router.beforeEach(to => to.path === '/billing' ? false : true)
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(wrapper.get('[role="alert"]').text()).toContain('已登录')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe('/dashboard')
  })

  it('登录成功后首页也无法进入时重试导航，不再次发送密码', async () => {
    const router = await renderLogin('/login?redirect=%2Fbilling')
    const replace = vi.spyOn(router, 'replace')
    replace.mockRejectedValueOnce(new Error('target failed')).mockRejectedValueOnce(new Error('home failed'))
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledOnce()
    expect(wrapper.get('[role="alert"]').text()).toContain('已登录')
    expect(wrapper.get('button[type="submit"]').text()).toContain('重新进入')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe('/dashboard')
  })

  it('登录后导航失败期间会话失效时恢复登录表单', async () => {
    const router = await renderLogin('/login?redirect=%2Fbilling')
    vi.spyOn(router, 'replace').mockRejectedValueOnce(new Error('target failed')).mockRejectedValueOnce(new Error('home failed'))
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    reactiveUser.token = ''
    window.dispatchEvent(new Event('prism:auth-expired'))
    await flushPromises()
    expect(wrapper.get('input[autocomplete="username"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('button[type="submit"]').text()).not.toContain('重新进入')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledTimes(2)
  })

  it('登录成功状态对读屏可读，不展示装饰性成功动画', async () => {
    const router = await renderLogin()
    let finishNavigation!: () => void
    vi.spyOn(router, 'replace').mockReturnValue(new Promise<void>((resolve) => { finishNavigation = resolve }))
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.get('[role="status"]').text()).toContain('登录成功'))

    const button = wrapper.get('.btn-login')
    expect(button.text()).toContain('正在登录')
    expect(wrapper.find('.login-success-mark').exists()).toBe(false)
    expect(wrapper.get('[role="status"]').text()).toContain('登录成功')
    expect(button.attributes('aria-busy')).toBe('true')

    finishNavigation()
    await flushPromises()
  })

  it('校验与请求期间重复提交只登录一次，忙态有可读文字', async () => {
    let finishLogin!: () => void
    user.login.mockReturnValue(new Promise<void>((resolve) => { finishLogin = resolve }))
    await renderLogin()
    await fillCredentials()
    await Promise.all([wrapper.get('form').trigger('submit'), wrapper.get('form').trigger('submit')])
    await flushPromises()
    expect(user.login).toHaveBeenCalledOnce()
    expect(wrapper.get('form').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('button[type="submit"]').text()).toContain('正在登录')
    expect(wrapper.get('[role="status"]').attributes('aria-live')).toBe('polite')
    finishLogin()
    await flushPromises()
    expect(wrapper.get('form').attributes('aria-busy')).toBe('false')
  })

  it('失败原因持久可读、保留输入，显式重试后成功', async () => {
    user.login.mockRejectedValueOnce({ message: '账号或密码不正确' })
    const router = await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('账号或密码不正确')
    expect((wrapper.get('input[autocomplete="username"]').element as HTMLInputElement).value).toBe('reviewer')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(wrapper.get('button[type="submit"]').text()).toContain('重新登录')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(router.currentRoute.value.path).toBe('/dashboard')
  })

  it('注册是原生可聚焦链接并通过既有路由跳转', async () => {
    const router = await renderLogin()
    const link = wrapper.get('a.link')
    ;(link.element as HTMLAnchorElement).focus()
    expect(document.activeElement).toBe(link.element)
    await link.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/register')
  })
})

describe('登录冷却倒计时', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
  })
  afterEach(() => {
    vi.useRealTimers()
    sessionStorage.clear()
  })

  it('安全校验不可用保留具体原因，五秒后恢复手动重试，不自动提交密码', async () => {
    user.login.mockRejectedValueOnce({ code: 50301, message: '安全校验服务暂不可用，请等待 5 秒后重试', retry_after_seconds: 5 })
    await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('安全校验服务暂不可用')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(5000)
    expect(user.login).toHaveBeenCalledOnce()
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledTimes(2)
  })

  it('按服务端秒数倒计时，冷却中直接提交不发请求，结束后仅手动重试', async () => {
    user.login.mockRejectedValueOnce({ code: 42900, message: '登录尝试过于频繁', retry_after_seconds: 37 })
    const router = await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('37 秒')
    expect(wrapper.get('[role="status"]').text()).toContain('正确密码')
    expect(wrapper.get('button[type="submit"]').text()).toContain('37 秒')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    expect(user.login).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(12_000)
    expect(wrapper.get('[role="status"]').text()).toContain('25 秒')
    await vi.advanceTimersByTimeAsync(25_000)
    expect(wrapper.get('[role="status"]').text()).toContain('可以重新登录')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(user.login).toHaveBeenCalledOnce()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(user.login).toHaveBeenCalledTimes(2)
    expect(router.currentRoute.value.path).toBe('/dashboard')
  })

  it('重新挂载保留同一等待截止时间，卸载清理计时器，不保存账号密码', async () => {
    user.login.mockRejectedValueOnce({ code: 42900, retry_after_seconds: 60, message: '限流' })
    await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(Object.values(sessionStorage).join('')).not.toMatch(/reviewer|test-password/)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(20_000)
    await renderLogin()
    expect(wrapper.get('[role="status"]').text()).toContain('40 秒')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(40_000)
    expect(wrapper.get('[role="status"]').text()).toContain('可以重新登录')
    expect(sessionStorage.length).toBe(0)
    expect(user.login).toHaveBeenCalledOnce()
  })

  it('没有服务端等待秒数时显示原因而不猜测倒计时', async () => {
    user.login.mockRejectedValueOnce({ code: 42900, message: '登录请求过于频繁，请等待后重试' })
    await renderLogin()
    await fillCredentials()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('请等待后重试')
    expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(sessionStorage.length).toBe(0)
  })
})
