import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  pricing: vi.fn(), wallet: vi.fn(), entitlement: vi.fn(), orders: vi.fn(), companyWallet: vi.fn(),
  enterpriseOrders: vi.fn(),
  createOrder: vi.fn(), refreshOrder: vi.fn(), cancelOrder: vi.fn(), getOrder: vi.fn(),
  getRefund: vi.fn(), requestRefund: vi.fn(), listEnterprises: vi.fn(), qr: vi.fn(),
}))
const account = vi.hoisted(() => ({ profile: { id: 1 }, token: null as string | null }))
vi.mock('@/api/billing', () => ({
  getBillingPricing: mock.pricing, getBillingWallet: mock.wallet, getBillingEntitlement: mock.entitlement,
  getBillingOrders: mock.orders, getEnterpriseBillingWallet: mock.companyWallet,
  getEnterpriseBillingOrders: mock.enterpriseOrders,
  createBillingOrder: mock.createOrder, refreshBillingOrder: mock.refreshOrder,
  cancelBillingOrder: mock.cancelOrder, getBillingOrder: mock.getOrder,
  getBillingRefund: mock.getRefund, requestBillingRefund: mock.requestRefund,
}))
vi.mock('@/api/enterprise', () => ({ listMyEnterprises: mock.listEnterprises }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})
vi.mock('qrcode', () => ({ default: { toDataURL: mock.qr } }))

import BillingCenter from './BillingCenter.vue'
import { useUserStore } from '@/stores/user'

const pricing = (salesEnabled: boolean) => ({
  currency: 'CNY' as const,
  credit_pack: { credits: 1000, price_fen: 400 },
  reviewer_month: { credits: 5000, price_fen: 4900 },
  enterprise_seat_month: { credits: 5000, price_fen: 6900 },
  model_milli_per_token: {}, billable_models: [],
  channels: { alipay: salesEnabled, wechat: salesEnabled }, sales_enabled: salesEnabled,
})

beforeEach(() => {
  useUserStore().profile!.id = 1
  useUserStore().token = ''
  localStorage.removeItem('review_token')
  sessionStorage.clear()
  Object.values(mock).forEach(fn => fn.mockReset())
  mock.pricing.mockResolvedValue(pricing(false))
  mock.wallet.mockResolvedValue({ available_milli: 1_234_000 })
  mock.entitlement.mockResolvedValue({ reviewer: false, enforcement_enabled: true })
  mock.orders.mockResolvedValue([])
  mock.companyWallet.mockResolvedValue({ available_milli: 2_500_000 })
  mock.enterpriseOrders.mockResolvedValue([])
  mock.listEnterprises.mockResolvedValue([])
  mock.qr.mockResolvedValue('data:image/png;base64,qr')
  mock.getRefund.mockResolvedValue(null)
})
afterEach(() => { vi.unstubAllGlobals() })

describe('计费中心', () => {
  it('切换订单时忽略前一订单迟到的退款查询', async () => {
    const makeOrder = (id: string) => ({ order_no: id, product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'paid', payment_status: 'paid', created_at: '2026-11-01T00:00:00' })
    const a = makeOrder('order-a'), b = makeOrder('order-b')
    mock.orders.mockResolvedValue([a, b])
    mock.getOrder.mockImplementation(async (id: string) => id === 'order-a' ? a : b)
    let finishA!: (value: unknown) => void
    mock.getRefund.mockReturnValueOnce(new Promise(resolve => { finishA = resolve })).mockResolvedValueOnce(null)
    const wrapper = mount(BillingCenter); await flushPromises()
    let buttons = wrapper.findAll('button').filter(item => item.text() === '查看')
    await buttons[0]!.trigger('click'); await flushPromises()
    buttons = wrapper.findAll('button').filter(item => item.text() === '查看')
    await buttons[1]!.trigger('click'); await flushPromises()
    finishA({ status: 'requested', reason: 'A 的退款原因' }); await flushPromises()
    expect(wrapper.text()).not.toContain('A 的退款原因')
    expect(wrapper.text()).toContain('提交退款申请')
    wrapper.unmount()
  })

  it('切换订单时忽略前一订单迟到的二维码', async () => {
    const makeOrder = (id: string) => ({ order_no: id, product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'created', payment_status: 'ready', payment_code_url: `alipay://${id}`,
      created_at: '2026-11-01T00:00:00' })
    const a = makeOrder('order-a'), b = makeOrder('order-b')
    mock.orders.mockResolvedValue([a, b])
    mock.getOrder.mockImplementation(async (id: string) => id === 'order-a' ? a : b)
    let finishA!: (value: string) => void
    mock.qr.mockReturnValueOnce(new Promise(resolve => { finishA = resolve })).mockResolvedValueOnce('qr-b')
    const wrapper = mount(BillingCenter); await flushPromises()
    let buttons = wrapper.findAll('button').filter(item => item.text() === '查看')
    await buttons[0]!.trigger('click'); await flushPromises()
    buttons = wrapper.findAll('button').filter(item => item.text() === '查看')
    await buttons[1]!.trigger('click'); await flushPromises()
    finishA('qr-a'); await flushPromises()
    expect(wrapper.get('img[alt="支付二维码，请使用对应支付应用扫码"]').attributes('src')).toBe('qr-b')
    expect(wrapper.get('.order-panel').text()).toContain('order-b')
    wrapper.unmount()
  })

  it('账号切换后忽略支付查单中迟到的旧个人余额', async () => {
    const pending = { order_no: 'order-a', product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'created', payment_status: 'ready', created_at: '2026-11-01T00:00:00' }
    mock.orders.mockResolvedValue([pending]); mock.getOrder.mockResolvedValue(pending)
    mock.refreshOrder.mockResolvedValue({ ...pending, status: 'paid', payment_status: 'paid' })
    let finishWallet!: (value: unknown) => void
    mock.wallet.mockResolvedValueOnce({ available_milli: 1_000_000 })
      .mockReturnValueOnce(new Promise(resolve => { finishWallet = resolve }))
      .mockResolvedValue({ available_milli: 9_000_000 })
    const wrapper = mount(BillingCenter); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '查看')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '查询支付状态')!.trigger('click'); await flushPromises()
    useUserStore().profile!.id = 2; await flushPromises()
    finishWallet({ available_milli: 1_000_000 }); await flushPromises()
    expect(wrapper.get('.summary-card strong').text()).toBe('9,000')
    wrapper.unmount()
  })

  it('企业切换后忽略查单中迟到的旧企业余额', async () => {
    mock.listEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'admin', status: 'active' },
      { id: 20, name: '乙公司', role: 'admin', status: 'active' },
    ])
    const order = { order_no: 'company-a', enterprise_id: 10, product: 'enterprise_credits', amount_fen: 400,
      quantity: 1, channel: 'alipay', status: 'paid', payment_status: 'paid', created_at: '2026-11-01T00:00:00' }
    mock.enterpriseOrders.mockImplementation(async (id: number) => id === 10 ? [order] : [])
    mock.getOrder.mockResolvedValue(order); mock.refreshOrder.mockResolvedValue(order)
    let finishWallet!: (value: unknown) => void
    mock.companyWallet.mockImplementation((id: number) => id === 20
      ? Promise.resolve({ available_milli: 9_000_000 })
      : mock.companyWallet.mock.calls.filter(call => call[0] === 10).length > 1
        ? new Promise(resolve => { finishWallet = resolve }) : Promise.resolve({ available_milli: 1_000_000 }))
    const wrapper = mount(BillingCenter); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业账户')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '查看')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '查询支付状态')!.trigger('click'); await flushPromises()
    await wrapper.get('select').setValue('20'); await flushPromises()
    finishWallet({ available_milli: 1_000_000 }); await flushPromises()
    expect(wrapper.text()).toContain('乙公司 企业积分9,000')
    wrapper.unmount()
  })

  it('会话存储不可用且下单结果未知时，同页面相同购买复用请求标识', async () => {
    mock.pricing.mockResolvedValue(pricing(true)); mock.createOrder.mockRejectedValue(new Error('timeout'))
    const get = Storage.prototype.getItem, set = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key: string) {
      if (this === sessionStorage) throw new Error('blocked')
      return get.call(this, key)
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (this === sessionStorage) throw new Error('blocked')
      set.call(this, key, value)
    })
    const wrapper = mount(BillingCenter); await flushPromises()
    const button = wrapper.findAll('button').find(item => item.text() === '创建支付订单')!
    await button.trigger('click'); await flushPromises()
    await button.trigger('click'); await flushPromises()
    expect(mock.createOrder).toHaveBeenCalledTimes(2)
    expect(mock.createOrder.mock.calls[0]![0].client_request_id).toBe(mock.createOrder.mock.calls[1]![0].client_request_id)
    expect(wrapper.text()).toContain('刷新页面后')
    wrapper.unmount()
    vi.restoreAllMocks()
  })

  it('展示个人余额，并在销售门关闭时禁用支付下单', async () => {
    const wrapper = mount(BillingCenter)
    await flushPromises()
    expect(wrapper.text()).toContain('1,234')
    expect(wrapper.text()).toContain('线上收款暂未开放')
    const button = wrapper.findAll('button').find(item => item.text() === '创建支付订单')!
    expect(button.attributes('disabled')).toBeDefined()
    expect(mock.createOrder).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('按服务端价格创建支付宝订单，展示二维码并只在查单确认后显示已支付', async () => {
    mock.pricing.mockResolvedValue(pricing(true))
    mock.createOrder.mockResolvedValue({
      order_no: 'pr_personal_1', product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'created', payment_status: 'ready', payment_code_url: 'alipay://pay/order-1',
      payment_expires_at: '2026-11-02T00:00:00', created_at: '2026-11-01T00:00:00',
    })
    mock.refreshOrder.mockResolvedValue({
      order_no: 'pr_personal_1', product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'paid', payment_status: 'paid', created_at: '2026-11-01T00:00:00',
    })
    const wrapper = mount(BillingCenter)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '创建支付订单')!.trigger('click')
    await flushPromises()
    expect(mock.createOrder).toHaveBeenCalledWith(expect.objectContaining({
      product: 'personal_credits', quantity: 1, channel: 'alipay', client_request_id: expect.any(String),
    }))
    expect(wrapper.get('img[alt="支付二维码，请使用对应支付应用扫码"]').attributes('src')).toBe('data:image/png;base64,qr')
    expect(wrapper.text()).toContain('等待支付')
    await wrapper.findAll('button').find(item => item.text() === '查询支付状态')!.trigger('click')
    await flushPromises()
    expect(mock.refreshOrder).toHaveBeenCalledWith('pr_personal_1')
    expect(wrapper.text()).toContain('已支付')
    expect(mock.wallet).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('企业购买席位包时传入企业名和席位数，不向用户端信任价格输入', async () => {
    mock.pricing.mockResolvedValue(pricing(true))
    mock.createOrder.mockResolvedValue({
      order_no: 'pr_enterprise_1', product: 'enterprise_create', amount_fen: 20700, quantity: 3,
      channel: 'wechat', status: 'created', payment_status: 'ready', payment_code_url: 'weixin://pay/order-1',
      created_at: '2026-11-01T00:00:00',
    })
    const wrapper = mount(BillingCenter)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业账户')!.trigger('click')
    await wrapper.get('input[autocomplete="organization"]').setValue('棱镜研发公司')
    await wrapper.get('input[type="number"]').setValue('3')
    await wrapper.findAll('button').find(item => item.text() === '微信支付')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '创建企业支付订单')!.trigger('click')
    await flushPromises()
    expect(mock.createOrder).toHaveBeenCalledWith(expect.objectContaining({
      product: 'enterprise_create', quantity: 3, channel: 'wechat', enterprise_name: '棱镜研发公司',
    }))
    expect(wrapper.text()).toContain('¥207.00')
    wrapper.unmount()
  })

  it('仅显示企业所有者或管理员的企业钱包入口', async () => {
    mock.listEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'owner', status: 'active' },
      { id: 20, name: '乙公司', role: 'member', status: 'active' },
    ])
    const wrapper = mount(BillingCenter)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业账户')!.trigger('click')
    expect(wrapper.text()).toContain('甲公司 企业积分')
    expect(wrapper.text()).not.toContain('乙公司 企业积分')
    expect(mock.companyWallet).toHaveBeenCalledWith(10)
    wrapper.unmount()
  })

  it('企业管理员可查看企业级订单并打开由其他管理员创建的企业订单', async () => {
    mock.listEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'admin', status: 'active' }])
    const row = {
      order_no: 'pr_company_order_1', enterprise_id: 10, product: 'enterprise_credits', amount_fen: 800,
      quantity: 2, channel: 'wechat', status: 'paid', payment_status: 'paid', created_at: '2026-11-01T00:00:00',
    }
    mock.enterpriseOrders.mockResolvedValue([row])
    mock.getOrder.mockResolvedValue(row)

    const wrapper = mount(BillingCenter)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业账户')!.trigger('click')
    await flushPromises()

    expect(mock.enterpriseOrders).toHaveBeenCalledWith(10)
    expect(wrapper.text()).toContain('甲公司 企业订单')
    expect(wrapper.text()).toContain('pr_company_order_1')
    await wrapper.findAll('button').find(item => item.text() === '查看')!.trigger('click')
    await flushPromises()
    expect(mock.getOrder).toHaveBeenCalledWith('pr_company_order_1')
    expect(wrapper.text()).toContain('当前订单')
    wrapper.unmount()
  })

  it('账号切换后丢弃旧账号迟到的付款二维码响应', async () => {
    mock.pricing.mockResolvedValue(pricing(true))
    let resolveOld!: (value: unknown) => void
    mock.createOrder.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(BillingCenter)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '创建支付订单')!.trigger('click')
    await vi.waitFor(() => expect(mock.createOrder).toHaveBeenCalledTimes(1))

    useUserStore().profile!.id = 2
    await vi.waitFor(() => expect(mock.pricing).toHaveBeenCalledTimes(2))
    resolveOld({
      order_no: 'pr_old_account', product: 'personal_credits', amount_fen: 400, quantity: 1,
      channel: 'alipay', status: 'created', payment_status: 'ready', payment_code_url: 'alipay://old-account',
      created_at: '2026-11-01T00:00:00',
    })
    await flushPromises()
    expect(wrapper.text()).not.toContain('pr_old_account')
    expect(mock.qr).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
