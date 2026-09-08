import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ checklist: vi.fn(), dashboard: vi.fn() }))
vi.mock('@/api/security', () => ({ getSecurityChecklist: mocks.checklist, getSecurityDashboard: mocks.dashboard }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: () => false }) }))
import SecurityCenter from './SecurityCenter.vue'

const emptyChecklist = { owasp_top10: [], secret_patterns: [], static_rules: [] }
const dashboard = { project_count: 7, scanned_project_count: 0, avg_risk_score: null }
function mountPage() {
  return mount(SecurityCenter, { global: {
    directives: { loading: () => {} },
    stubs: {
      SecurityScanModal: true,
      'el-icon': { template: '<span><slot /></span>' },
      'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' },
      'el-dialog': true,
      'el-table': true,
      'el-table-column': true,
      'el-tag': true,
    },
  } })
}
beforeEach(() => {
  mocks.checklist.mockReset().mockResolvedValue(emptyChecklist)
  mocks.dashboard.mockReset().mockResolvedValue(dashboard)
})

describe('安全中心独立读取恢复', () => {
  it('概览失败保留常驻错误和请求ID，不影响知识库并能真实重试', async () => {
    mocks.dashboard.mockRejectedValueOnce({ message: '上游暂不可用', request_id: 'req-security-1', next_action: '稍后重试读取' })
    const page = mountPage()
    await flushPromises()
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('上游暂不可用')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('req-security-1')
    expect(page.text()).toContain('OWASP Top 10')
    expect(page.text()).not.toContain('还没有项目可分析')
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(mocks.dashboard).toHaveBeenCalledTimes(2)
    expect(page.find('[data-testid="dashboard-error"]').exists()).toBe(false)
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    page.unmount()
  })

  it('清单失败不能把未知数量显示成0，重试成功的空清单才显示0与空态', async () => {
    mocks.checklist.mockRejectedValueOnce(new Error('网络中断'))
    const page = mountPage()
    await flushPromises()
    expect(page.get('[data-testid="checklist-error"]').text()).toContain('网络中断')
    expect(page.findAll('.hl').map(item => item.text())).toEqual(['—', '—'])
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    await page.get('[data-testid="refresh-checklist"]').trigger('click')
    await flushPromises()
    expect(page.findAll('.hl').map(item => item.text())).toEqual(['0', '0'])
    expect(page.text()).toContain('未配置敏感信息规则')
    expect(page.text()).toContain('未配置静态语义规则')
    page.unmount()
  })

  it('重复点击重试只发一个请求，结束后恢复按钮', async () => {
    let resolve!: (value: typeof dashboard) => void
    mocks.dashboard.mockReturnValue(new Promise(done => { resolve = done }))
    const page = mountPage()
    await flushPromises()
    const button = page.get('[data-testid="refresh-dashboard"]')
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click')
    expect(mocks.dashboard).toHaveBeenCalledTimes(1)
    resolve(dashboard)
    await flushPromises()
    expect(button.attributes('disabled')).toBeUndefined()
    page.unmount()
  })

  it.each([40101, 40301, 40401])('失权或资源消失 %s 清除旧概览', async code => {
    const page = mountPage()
    await flushPromises()
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    mocks.dashboard.mockRejectedValueOnce({ code, message: '无法访问' })
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(page.text()).not.toContain('7 个项目还未进行安全审计')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('无法访问')
    page.unmount()
  })

  it('暂时错误保留上次成功结果并明确未更新', async () => {
    const page = mountPage()
    await flushPromises()
    mocks.dashboard.mockRejectedValueOnce(new Error('连接超时'))
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('保留上次成功结果')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('尚未更新')
    page.unmount()
  })
})
