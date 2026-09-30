import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  confirm: vi.fn(),
  logout: vi.fn(),
  push: vi.fn(),
  route: { path: '/admin/overview', fullPath: '/admin/overview', meta: { title: '运行总览' } },
}))

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-router')>()
  return {
    ...actual,
    useRoute: () => auth.route,
    useRouter: () => ({ push: auth.push }),
  }
})
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    displayName: '测试管理员',
    isSuperAdmin: () => true,
    logout: auth.logout,
  }),
}))
vi.mock('@/utils/agentNavigation', () => ({ isNavigationPathAllowed: () => true }))
vi.mock('@/components/ai/ProactivePageGuide.vue', () => ({ default: { template: '<div />' } }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: auth.confirm },
}))

import AdminLayout from './AdminLayout.vue'

it('管理员确认退出按钮文案为“确定”并完成退出流程', async () => {
  auth.confirm.mockResolvedValue(undefined)
  auth.logout.mockResolvedValue(undefined)

  const wrapper = mount(AdminLayout, {
    global: {
      stubs: {
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-dropdown-item': { emits: ['click'], template: '<button @click="$emit(\'click\')"><slot /></button>' },
        'el-tag': { template: '<span><slot /></span>' },
        'el-icon': { template: '<span><slot /></span>' },
        'router-view': { template: '<div />' },
      },
    },
  })

  await wrapper.findAll('.admin-user button').find(button => button.text().includes('退出登录'))!.trigger('click')
  await flushPromises()

  expect(auth.confirm).toHaveBeenCalledWith('确定要退出管理后台吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning',
  })
  expect(auth.logout).toHaveBeenCalledOnce()
  expect(auth.push).toHaveBeenCalledWith('/login')
  wrapper.unmount()
})
