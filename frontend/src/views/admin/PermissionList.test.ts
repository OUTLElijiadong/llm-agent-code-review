import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ listPermissions: vi.fn() }))
vi.mock('@/api/rbac', () => ({ listPermissions: api.listPermissions }))

import PermissionList from './PermissionList.vue'

function mountPage() {
  return shallowMount(PermissionList, {
    global: {
      stubs: {
        'el-alert': { props: ['title'], template: '<div role="alert">{{ title }}<slot /></div>' },
        'el-button': true,
        'el-card': { template: '<div><slot /></div>' },
        'el-empty': { props: ['description'], template: '<div>{{ description }}</div>' },
        'el-input': true,
        'el-icon': true,
        'el-tag': true,
      },
      directives: { loading: () => undefined },
    },
  })
}

describe('PermissionList 目录状态', () => {
  beforeEach(() => vi.clearAllMocks())

  it('权限接口失败时显示错误而不把失败伪装成零权限', async () => {
    api.listPermissions.mockRejectedValueOnce(new Error('network'))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('权限点加载失败')
    expect(wrapper.text()).toContain('共 — 个')
    expect(wrapper.text()).not.toContain('权限接口返回 0 个权限点')
    wrapper.unmount()
  })

  it('接口真实返回空数组时明确展示空目录状态', async () => {
    api.listPermissions.mockResolvedValueOnce([])
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('共 0 个')
    expect(wrapper.text()).toContain('权限接口返回 0 个权限点')
    wrapper.unmount()
  })
})
