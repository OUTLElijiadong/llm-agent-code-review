import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  permissions: new Set<string>(),
  admin: false,
}))
const api = vi.hoisted(() => ({
  getRules: vi.fn(),
  toggleRule: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  deleteRule: vi.fn(),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    isAdmin: () => auth.admin,
    hasPermission: (code: string) => auth.admin || auth.permissions.has(code),
  }),
}))
vi.mock('@/api/rule', () => api)
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn() },
}))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn() },
}))

import RuleConfig from './RuleConfig.vue'

const ButtonStub = {
  inheritAttrs: false,
  template: '<button v-bind="$attrs"><slot /></button>',
}
const SwitchStub = {
  inheritAttrs: false,
  template: '<button class="switch-stub" v-bind="$attrs"></button>',
}
const GenericStub = { template: '<div><slot /></div>' }
const TableColumnStub = {
  template: `<div><slot :row="{
    id: 1,
    rule_code: 'custom_rule',
    rule_name: '自定义规则',
    rule_type: 'security',
    rule_content: '检查',
    language: '*',
    severity: '中',
    enabled: 1,
    is_builtin: 0,
    sort_order: 1
  }" /></div>`,
}

function mountPage() {
  return shallowMount(RuleConfig, {
    global: {
      stubs: {
        'el-button': ButtonStub,
        'el-switch': SwitchStub,
        'el-table': GenericStub,
        'el-table-column': TableColumnStub,
        'el-card': GenericStub,
        'el-input': GenericStub,
        'el-tag': GenericStub,
        'el-dialog': GenericStub,
        'el-form': GenericStub,
        'el-form-item': GenericStub,
        'el-select': GenericStub,
        'el-option': GenericStub,
        'el-icon': GenericStub,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  auth.permissions = new Set()
  auth.admin = false
  api.getRules.mockReset()
  api.getRules.mockResolvedValue([])
})

describe('RuleConfig granular permissions', () => {
  it('只读账号不显示新增、启停、编辑和删除操作', async () => {
    auth.permissions.add('rule:view')
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).not.toContain('新增规则')
    expect(wrapper.find('.switch-stub').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('编辑')
    expect(wrapper.text()).not.toContain('删除')
  })

  it('只显示账号实际拥有的规则操作', async () => {
    auth.permissions = new Set(['rule:create', 'rule:update'])
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('新增规则')
    expect(wrapper.find('.switch-stub').exists()).toBe(true)
    expect(wrapper.text()).toContain('编辑')
    expect(wrapper.text()).not.toContain('删除')
  })
})
