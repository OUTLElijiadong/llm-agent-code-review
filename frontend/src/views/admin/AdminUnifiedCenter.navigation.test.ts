import { flushPromises, shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const navigation = vi.hoisted(() => ({
  route: { path: '/admin/governance', meta: { unifiedDomain: 'agents' }, query: { section: 'agents' } },
  replace: vi.fn(),
  superAdmin: true,
}))

vi.mock('vue-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('vue-router')>(),
  useRoute: () => navigation.route,
  useRouter: () => ({ replace: navigation.replace }),
}))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isSuperAdmin: () => navigation.superAdmin }) }))

import AdminUnifiedCenter from './AdminUnifiedCenter.vue'

describe('Agent 治理中心页签导航', () => {
  it('点击发布审批时留在治理中心路由，并切换到 releases', async () => {
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
    await flushPromises()
    await vi.dynamicImportSettled()
    wrapper.unmount()
  })

  it('渗透执行核查只向最高管理员显示', async () => {
    navigation.route.path = '/admin/operations'
    navigation.route.meta.unifiedDomain = 'operations'
    navigation.route.query.section = 'pentest-evidence'
    const options = { global: { stubs: {
      'el-tabs': { name: 'ElTabs', template: '<div><slot /></div>' },
      'el-tab-pane': { name: 'ElTabPane', props: ['label', 'name'], template: '<span>{{ label }}</span>' },
      'el-empty': { name: 'ElEmpty', template: '<div />' },
    } } }
    navigation.superAdmin = false
    const ordinary = shallowMount(AdminUnifiedCenter, options)
    expect(ordinary.text()).not.toContain('渗透执行核查')
    await flushPromises()
    await vi.dynamicImportSettled()
    ordinary.unmount()
    navigation.superAdmin = true
    const administrator = shallowMount(AdminUnifiedCenter, options)
    expect(administrator.text()).toContain('渗透执行核查')
    await flushPromises()
    await vi.dynamicImportSettled()
    administrator.unmount()
    navigation.route.path = '/admin/governance'
    navigation.route.meta.unifiedDomain = 'agents'
    navigation.route.query.section = 'agents'
  })
})
