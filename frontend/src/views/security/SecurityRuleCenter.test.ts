import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
const state = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  replace: vi.fn(),
  push: vi.fn(),
  ruleView: true,
  securityView: true,
}))

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-router')>()
  return {
    ...actual,
    useRoute: () => state.route,
    useRouter: () => ({ replace: state.replace, push: state.push }),
  }
})
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    hasPermission: (code: string) => code === 'rule:view' ? state.ruleView : state.securityView,
  }),
}))
vi.mock('./SecurityCenter.vue', () => ({
  default: { props: ['embedded'], template: '<div data-testid="posture">posture</div>' },
}))
vi.mock('@/views/rule/RuleConfig.vue', () => ({
  default: { props: ['embedded'], template: '<div data-testid="rules">rules</div>' },
}))
vi.mock('./SecurityRuleCatalog.vue', () => ({
  default: { template: '<div data-testid="catalog">catalog</div>' },
}))

import SecurityRuleCenter from './SecurityRuleCenter.vue'

const GenericStub = { template: '<div><slot /><slot name="label" /></div>' }

function mountPage() {
  return mount(SecurityRuleCenter, {
    global: {
      stubs: {
        'el-tabs': GenericStub,
        'el-tab-pane': GenericStub,
        'el-icon': GenericStub,
      },
    },
  })
}

beforeEach(() => {
  state.route.query = {}
  state.replace.mockReset()
  state.push.mockReset()
  state.ruleView = true
  state.securityView = true
})

describe('安全与审查规则统一页面', () => {
  it('在同一页面提供态势、规则管理和统一目录', () => {
    const page = mountPage()
    expect(page.text()).toContain('安全与审查规则')
    expect(page.text()).toContain('安全态势与规则基座')
    expect(page.text()).toContain('审查规则管理')
    expect(page.text()).toContain('统一规则目录')
  })

  it('无 rule:view 时不渲染规则管理标签', async () => {
    state.ruleView = false
    const page = mountPage()
    await flushPromises()
    expect(page.text()).not.toContain('审查规则管理')
    expect(page.text()).toContain('统一规则目录')
  })

  it('只有 rule:view 时仍可通过统一页面管理规则', async () => {
    state.securityView = false
    const page = mountPage()
    await flushPromises()
    expect(page.text()).not.toContain('安全态势与规则基座')
    expect(page.text()).not.toContain('统一规则目录')
    expect(page.text()).toContain('审查规则管理')
  })
})
