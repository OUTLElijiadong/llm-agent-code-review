import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { AxiosError, type AxiosResponse } from 'axios'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const ui = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), replace: vi.fn(), push: vi.fn() }))
vi.mock('@/router', () => ({ default: { replace: ui.replace, push: ui.push } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: ui.push }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: ui.error, success: ui.success } }))

import http from '@/api/http'
import { useUserStore } from '@/stores/user'
import { getToken, setToken } from '@/utils/token'
import AiLogList from './AiLogList.vue'
import SystemAudit from './SystemAudit.vue'

const originalAdapter = http.defaults.adapter
const calls: { url: string | undefined; authenticated: boolean }[] = []

beforeEach(() => {
  localStorage.clear()
  delete window.__prismAuthExpiredHandled
  calls.length = 0
  setActivePinia(createPinia())
  setToken('local-fixture-session')
  const user = useUserStore()
  user.profile = { id: 101, username: 'local-admin', role: 'admin', status: 1 }
  http.defaults.adapter = async (config) => {
    const authenticated = Boolean(config.headers.get('Authorization'))
    calls.push({ url: config.url, authenticated })
    if (!authenticated) {
      throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
        config, status: 401, statusText: 'Unauthorized', headers: {},
        data: { code: 40100, message: '缺少token', data: null },
      })
    }
    return { config, status: 200, statusText: 'OK', headers: {}, data: { code: 0, message: 'ok', data: { items: [], total: 0 } } }
  }
})

afterEach(() => { http.defaults.adapter = originalAdapter })

function render(component: typeof AiLogList | typeof SystemAudit) {
  return mount(component, { global: {
    directives: { loading: () => undefined },
    stubs: {
      'el-card': { template: '<div><slot /></div>' }, 'el-select': true, 'el-option': true,
      'el-date-picker': true, 'el-input': true, 'el-checkbox': true,
      'el-tag': { template: '<span><slot /></span>' }, 'el-button': true,
      'el-icon': true, 'el-pagination': true,
    },
  } })
}

const pages = [
  { page: 'AI 调用日志', component: AiLogList, url: '/ai-logs' },
  { page: '操作审计', component: SystemAudit, url: '/admin/audit' },
] as const

it.each(pages.flatMap(page => [1, 2, 3].map(sample => ({ ...page, sample }))))('$page 样本$sample 正常退出不能派发匿名后台请求或提示缺少 token', async ({ component, url }) => {
  const user = useUserStore()
  const wrapper = render(component)
  await flushPromises()
  expect(calls).toEqual([{ url, authenticated: true }])

  user.logout()
  await flushPromises()

  expect(getToken()).toBeNull()
  expect.soft(calls).toEqual([{ url, authenticated: true }])
  expect.soft(ui.error).not.toHaveBeenCalled()
  expect.soft(ui.replace).not.toHaveBeenCalled()
  wrapper.unmount()
})

it.each(pages)('$page 匿名初始挂载不请求受限日志', async ({ component }) => {
  useUserStore().logout()
  const wrapper = render(component)
  await flushPromises()
  expect(calls).toEqual([])
  expect(ui.error).not.toHaveBeenCalled()
  wrapper.unmount()
})

it.each(pages.flatMap(page => ['token', 'profile'].map(missing => ({ ...page, missing }))))('$page 缺少$missing时不重新加载且清空旧列表', async ({ component, url, missing }) => {
  const user = useUserStore()
  const wrapper = render(component)
  await flushPromises()
  if (missing === 'token') user.token = ''
  else user.profile = null
  await flushPromises()
  expect(calls).toEqual([{ url, authenticated: true }])
  expect(ui.error).not.toHaveBeenCalled()
  wrapper.unmount()
})

it.each(pages.flatMap(page => ['success', '401'].map(outcome => ({ ...page, outcome }))))('$page 退出后的旧$outcome响应不恢复旧数据或误报退出失败', async ({ component, url, outcome }) => {
  let settle!: () => void
  http.defaults.adapter = (config) => new Promise<AxiosResponse>((resolve, reject) => {
    calls.push({ url: config.url, authenticated: Boolean(config.headers.get('Authorization')) })
    settle = () => outcome === '401'
      ? reject(new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
        config, status: 401, statusText: 'Unauthorized', headers: {},
        data: { code: 40100, message: '缺少token', data: null },
      }))
      : resolve({ config, status: 200, statusText: 'OK', headers: {}, data: { code: 0, message: 'ok', data: {
        items: [{ id: 1, actor_name: '旧账号内容', model_name: '旧账号内容', status: 'success', action: 'login' }], total: 1,
      } } })
  })
  const wrapper = render(component)
  await flushPromises()
  useUserStore().logout()
  settle()
  await flushPromises()
  expect(calls).toEqual([{ url, authenticated: true }])
  expect(wrapper.text()).not.toContain('旧账号内容')
  expect(ui.error).not.toHaveBeenCalled()
  expect(ui.replace).not.toHaveBeenCalled()
  wrapper.unmount()
})

it.each(pages)('$page 换账号仍刷新当前账号日志', async ({ component }) => {
  const user = useUserStore()
  const wrapper = render(component)
  await flushPromises()
  http.defaults.adapter = async (config) => {
    calls.push({ url: config.url, authenticated: Boolean(config.headers.get('Authorization')) })
    return { config, status: 200, statusText: 'OK', headers: {}, data: { code: 0, message: 'ok', data: {
      items: [{ id: 2, actor_name: '新账号内容', model_name: '新账号内容', status: 'success', action: 'login' }], total: 1,
    } } }
  }
  setToken('local-next-session')
  user.token = 'local-next-session'
  user.profile = { id: 202, username: 'local-next-admin', role: 'admin', status: 1 }
  await flushPromises()
  expect(calls.every(call => call.authenticated)).toBe(true)
  expect(wrapper.text()).toContain('新账号内容')
  expect(ui.error).not.toHaveBeenCalled()
  wrapper.unmount()
})
