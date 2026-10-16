import { shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const navigation = vi.hoisted(() => ({
  route: { path: '/admin/governance', meta: { unifiedDomain: 'agents' }, query: { section: 'agents' } },
  replace: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => navigation.route,
  useRouter: () => ({ replace: navigation.replace }),
}))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isSuperAdmin: () => true }) }))

import AdminUnifiedCenter from './AdminUnifiedCenter.vue'

describe('Agent 治理中心页签导航', () => {
  it('点击发布审批时留在治理中心路由，并切换到 releases', () => {
    navigation.replace.mockClear()
    const wrapper = shallowMount(AdminUnifiedCenter, {
      global: {
        stubs: {
          'el-tabs': { name: 'ElTabs', template: '<div><slot /></div>' },
          'el-tab-pane': { name: 'ElTabPane', template: '<div />' },
          'el-empty': { name: 'ElEmpty', template: '<div />' },
        },
      },
    })

    wrapper.getComponent({ name: 'ElTabs' }).vm.$emit('tab-change', 'releases')

    expect(navigation.replace).toHaveBeenCalledWith({
      path: '/admin/governance',
      query: { section: 'releases' },
    })
    wrapper.unmount()
  })
})
