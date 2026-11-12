<template>
  <main class="billing-page" :aria-busy="loading">
    <header class="page-head">
      <div>
        <h1>充值与订阅</h1>
        <p>按服务端商品价格创建支付宝或微信支付订单；支付确认后才发放积分、会员或企业席位。</p>
      </div>
      <button type="button" :disabled="loading" @click="() => load()">刷新</button>
    </header>

    <p v-if="!pricing?.sales_enabled" class="gate-note" role="status">
      线上收款暂未开放。平台计量、权益、商户配置和验收门禁未全部通过；当前无法创建支付订单。
    </p>
    <p v-if="pageError" class="error-note" role="alert">{{ pageError }}</p>
    <p v-if="orderError && !activeOrder" class="error-note" role="alert">{{ orderError }}</p>
    <p v-if="loading && !pricing" class="notice" role="status">正在加载账户与商品信息…</p>
        <p v-else-if="pageError && !pricing" class="notice" role="alert">
      计费信息加载失败。<button type="button" @click="() => load()">重新加载</button>
    </p>

    <template v-if="pricing">
      <section class="summary-grid" aria-label="账户余额与权益">
        <article class="summary-card">
          <span>个人积分余额</span>
          <strong>{{ number((wallet?.available_milli ?? 0) / 1000) }}</strong>
          <small>积分 · 未知用量不会按零扣费</small>
        </article>
        <article class="summary-card">
          <span>个人审查员</span>
          <strong>{{ entitlement?.reviewer ? '已开通' : '未开通' }}</strong>
          <small>{{ entitlement?.enforcement_enabled ? '权益校验已启用' : '权益校验当前为兼容模式' }}</small>
        </article>
        <article v-if="selectedEnterprise" class="summary-card">
          <span>{{ selectedEnterprise.name }} 企业积分</span>
          <strong>{{ enterpriseWallet ? number(enterpriseWallet.available_milli / 1000) : '—' }}</strong>
          <small>仅企业所有者或管理员可查看和充值</small>
        </article>
      </section>

      <section class="purchase card">
        <div class="tabs" role="tablist" aria-label="购买范围">
          <button type="button" role="tab" :aria-selected="purchaseScope === 'personal'" @click="purchaseScope = 'personal'">个人账户</button>
          <button type="button" role="tab" :aria-selected="purchaseScope === 'enterprise'" @click="purchaseScope = 'enterprise'">企业账户</button>
        </div>

        <template v-if="purchaseScope === 'personal'">
          <h2>个人商品</h2>
          <label class="field"><span>购买内容</span>
            <select v-model="personalProduct">
              <option value="personal_credits">积分充值包 · {{ number(pricing.credit_pack.credits) }} 积分 / {{ money(pricing.credit_pack.price_fen) }}</option>
              <option value="reviewer_month">审查员月度会员 · {{ number(pricing.reviewer_month.credits) }} 积分 / {{ money(pricing.reviewer_month.price_fen) }}</option>
            </select>
          </label>
          <label v-if="personalProduct === 'personal_credits'" class="field"><span>充值包数量</span>
            <input v-model.number="quantity" type="number" min="1" max="100" step="1" inputmode="numeric" />
          </label>
          <p v-else class="muted">会员权益按自然月周期发放。已有永久审查员权益不会因购买订单而重复升级。</p>
          <p class="price">应付金额 <strong>{{ money(personalTotalFen) }}</strong></p>
          <div class="channels" role="group" aria-label="选择支付渠道">
            <button type="button" :aria-pressed="channel === 'alipay'" :disabled="!pricing.channels.alipay" @click="channel = 'alipay'">支付宝</button>
            <button type="button" :aria-pressed="channel === 'wechat'" :disabled="!pricing.channels.wechat" @click="channel = 'wechat'">微信支付</button>
          </div>
          <button class="primary" type="button" :disabled="!canCreatePersonal" @click="createPersonalOrder">
            {{ creating ? '正在创建订单…' : '创建支付订单' }}
          </button>
        </template>

        <template v-else>
          <h2>企业席位与积分</h2>
          <label v-if="managedEnterprises.length" class="field"><span>管理企业</span>
            <select v-model.number="selectedEnterpriseId">
              <option v-for="company in managedEnterprises" :key="company.id" :value="company.id">{{ company.name }}</option>
            </select>
          </label>
          <template v-if="selectedEnterprise">
            <label class="field"><span>购买内容</span>
              <select v-model="enterpriseProduct">
                <option value="enterprise_renew">续费企业席位（按下一周期总席位计价）</option>
                <option value="enterprise_credits">企业积分充值包</option>
              </select>
            </label>
            <label v-if="enterpriseProduct === 'enterprise_renew'" class="field"><span>下一周期席位总数</span>
              <input v-model.number="quantity" type="number" min="1" max="10000" step="1" inputmode="numeric" />
              <small>缩减席位不会删除现有成员；席位数仅在续费周期开始时生效。</small>
            </label>
            <label v-else class="field"><span>积分充值包数量</span>
              <input v-model.number="quantity" type="number" min="1" max="100" step="1" inputmode="numeric" />
            </label>
            <p class="price">应付金额 <strong>{{ money(enterpriseTotalFen) }}</strong></p>
            <div class="channels" role="group" aria-label="选择支付渠道">
              <button type="button" :aria-pressed="channel === 'alipay'" :disabled="!pricing.channels.alipay" @click="channel = 'alipay'">支付宝</button>
              <button type="button" :aria-pressed="channel === 'wechat'" :disabled="!pricing.channels.wechat" @click="channel = 'wechat'">微信支付</button>
            </div>
            <button class="primary" type="button" :disabled="!canCreateEnterprise" @click="createEnterpriseOrder">
              {{ creating ? '正在创建订单…' : '创建企业支付订单' }}
            </button>
          </template>
          <template v-else>
            <p class="muted">当前账号没有可管理的企业。购买企业席位包后，支付确认时会创建企业并将你设为所有者。</p>
            <label class="field"><span>公司名称</span>
              <input v-model.trim="enterpriseName" maxlength="120" autocomplete="organization" placeholder="输入公司名称" />
            </label>
            <label class="field"><span>首期席位数</span>
              <input v-model.number="quantity" type="number" min="1" max="10000" step="1" inputmode="numeric" />
              <small>{{ number(pricing.enterprise_seat_month.credits) }} 积分/席位/周期，价格 {{ money(pricing.enterprise_seat_month.price_fen) }}/席位。</small>
            </label>
            <p class="price">应付金额 <strong>{{ money(enterpriseCreateTotalFen) }}</strong></p>
            <div class="channels" role="group" aria-label="选择支付渠道">
              <button type="button" :aria-pressed="channel === 'alipay'" :disabled="!pricing.channels.alipay" @click="channel = 'alipay'">支付宝</button>
              <button type="button" :aria-pressed="channel === 'wechat'" :disabled="!pricing.channels.wechat" @click="channel = 'wechat'">微信支付</button>
            </div>
            <button class="primary" type="button" :disabled="!canCreateEnterprise" @click="createEnterpriseOrder">
              {{ creating ? '正在创建订单…' : '创建企业支付订单' }}
            </button>
          </template>
        </template>
      </section>

      <section v-if="purchaseScope === 'enterprise' && selectedEnterprise" class="card orders-panel">
        <div class="section-head">
          <h2>{{ selectedEnterprise.name }} 企业订单</h2>
          <button type="button" :disabled="loadingEnterpriseOrders" @click="refreshEnterpriseOrders">
            {{ loadingEnterpriseOrders ? '正在刷新…' : '刷新企业订单' }}
          </button>
        </div>
        <p v-if="enterpriseOrderError" class="error-note" role="alert">{{ enterpriseOrderError }}</p>
        <p v-if="loadingEnterpriseOrders" role="status">正在加载企业账单…</p>
        <p v-else-if="enterpriseOrders.length === 0" class="muted">该企业暂无已记录订单。</p>
        <div v-else class="table-wrap">
          <table>
            <thead><tr><th>订单号</th><th>商品</th><th>金额</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody><tr v-for="order in enterpriseOrders" :key="order.order_no">
              <td>{{ order.order_no }}</td><td>{{ productName(order.product) }}</td><td>{{ money(order.amount_fen) }}</td>
              <td>{{ orderStatus(order) }}</td><td>{{ dateTime(order.created_at) }}</td>
              <td><button type="button" @click="selectOrder(order.order_no)">查看</button></td>
            </tr></tbody>
          </table>
        </div>
        <p class="muted">企业账单对企业所有者和管理员可见；员工只能查看自己发起的订单。</p>
      </section>

      <section v-if="activeOrder" class="card order-panel" aria-live="polite">
        <div class="section-head"><h2>当前订单</h2><button type="button" :disabled="orderLoading" @click="refreshActiveOrder">查询支付状态</button></div>
        <dl>
          <div><dt>订单号</dt><dd>{{ activeOrder.order_no }}</dd></div>
          <div><dt>商品</dt><dd>{{ productName(activeOrder.product) }}</dd></div>
          <div><dt>应付</dt><dd>{{ money(activeOrder.amount_fen) }}</dd></div>
          <div><dt>状态</dt><dd>{{ orderStatus(activeOrder) }}</dd></div>
          <div v-if="activeOrder.payment_expires_at"><dt>付款有效期</dt><dd>{{ dateTime(activeOrder.payment_expires_at) }}</dd></div>
        </dl>
        <div v-if="activeOrder.payment_code_url && activeOrder.status !== 'paid' && activeOrder.status !== 'refunded'" class="qr-area">
          <img v-if="qrDataUrl" :src="qrDataUrl" alt="支付二维码，请使用对应支付应用扫码" />
          <p v-if="qrError" class="error-note" role="alert">支付二维码生成失败，可在支持二维码扫描的移动设备上打开支付链接。</p>
          <a :href="activeOrder.payment_code_url">打开支付链接</a>
          <button type="button" @click="copyPaymentCode">复制支付链接</button>
          <p>请使用{{ activeOrder.channel === 'alipay' ? '支付宝' : '微信' }}扫码。页面不会因扫码成功而提前发放权益；请点击“查询支付状态”核验渠道结果。</p>
        </div>
        <button v-if="canCancelActiveOrder" type="button" :disabled="orderLoading" @click="cancelActiveOrder">关闭未支付订单</button>
        <form v-if="activeOrder.status === 'paid' && !activeRefund" class="refund-form" @submit.prevent="submitRefund">
          <label class="field"><span>申请退款原因</span><textarea v-model.trim="refundReason" maxlength="500" rows="2" placeholder="说明退款原因" /></label>
          <p class="muted">退款会由平台审核。已消费、已预占积分或订阅/席位订单不会自动退款。</p>
          <button type="submit" :disabled="orderLoading || !refundReason.trim()">提交退款申请</button>
        </form>
        <div v-if="activeRefund" class="refund-status">
          <strong>退款状态：{{ refundStatus(activeRefund.status) }}</strong>
          <p>{{ activeRefund.review_note || activeRefund.reason }}</p>
          <button v-if="activeRefund.status === 'succeeded' || activeRefund.status === 'rejected'" type="button" @click="activeRefund = null">关闭</button>
        </div>
        <p v-if="orderError" class="error-note" role="alert">{{ orderError }}</p>
      </section>

      <section class="card orders-panel">
        <div class="section-head"><h2>我的订单</h2><button type="button" :disabled="loadingOrders" @click="() => loadOrders()">刷新订单</button></div>
        <p v-if="loadingOrders" role="status">正在加载订单…</p>
        <p v-else-if="orders.length === 0" class="muted">暂无由当前账号创建的订单。</p>
        <div v-else class="table-wrap">
          <table>
            <thead><tr><th>订单号</th><th>商品</th><th>金额</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody><tr v-for="order in orders" :key="order.order_no">
              <td>{{ order.order_no }}</td><td>{{ productName(order.product) }}</td><td>{{ money(order.amount_fen) }}</td>
              <td>{{ orderStatus(order) }}</td><td>{{ dateTime(order.created_at) }}</td>
              <td><button type="button" @click="selectOrder(order.order_no)">查看</button></td>
            </tr></tbody>
          </table>
        </div>
        <p class="muted">订单列表仅显示当前账号作为付款人的订单；企业账务以服务端授权和账单记录为准。</p>
      </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import QRCode from 'qrcode'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { listMyEnterprises, type Enterprise } from '@/api/enterprise'
import {
  cancelBillingOrder, createBillingOrder, getBillingEntitlement, getBillingOrder, getBillingOrders,
  getBillingPricing, getBillingRefund, getBillingWallet, getEnterpriseBillingOrders,
  getEnterpriseBillingWallet, refreshBillingOrder, requestBillingRefund,
  type BillingOrder, type BillingPricing, type BillingRefund,
} from '@/api/billing'
import { getToken } from '@/utils/token'

const route = useRoute()
const user = useUserStore()
const currentUserId = computed(() => Number(user.profile?.id ?? 0))
const pricing = ref<BillingPricing | null>(null)
const wallet = ref<{ available_milli: number } | null>(null)
const entitlement = ref<{ reviewer: boolean; enforcement_enabled: boolean } | null>(null)
const enterprises = ref<Enterprise[]>([])
const enterpriseWallet = ref<{ available_milli: number } | null>(null)
const orders = ref<BillingOrder[]>([])
const enterpriseOrders = ref<BillingOrder[]>([])
const loadingEnterpriseOrders = ref(false)
const enterpriseOrderError = ref('')
const activeOrder = ref<BillingOrder | null>(null)
const activeRefund = ref<BillingRefund | null>(null)
const qrDataUrl = ref('')
const qrError = ref(false)
const loading = ref(false)
const loadingOrders = ref(false)
const creating = ref(false)
const orderLoading = ref(false)
const pageError = ref('')
const orderError = ref('')
const purchaseScope = ref<'personal' | 'enterprise'>('personal')
const personalProduct = ref<'personal_credits' | 'reviewer_month'>('personal_credits')
const enterpriseProduct = ref<'enterprise_renew' | 'enterprise_credits'>('enterprise_renew')
const selectedEnterpriseId = ref<number | null>(null)
const enterpriseName = ref('')
const quantity = ref(1)
const channel = ref<'alipay' | 'wechat'>('alipay')
const refundReason = ref('')
let generation = 0
let orderGeneration = 0
let enterpriseWalletGeneration = 0
let enterpriseOrderGeneration = 0
const pendingOrderKeys = new Map<string, string>()
let pendingOrderStorageUnavailable = false

const managedEnterprises = computed(() => enterprises.value.filter(item => item.role === 'owner' || item.role === 'admin'))
const selectedEnterprise = computed(() => managedEnterprises.value.find(item => item.id === selectedEnterpriseId.value) ?? null)
const quantityMaximum = computed(() => {
  if (purchaseScope.value === 'personal') return 100
  if (!selectedEnterprise.value || enterpriseProduct.value === 'enterprise_renew') return 10000
  return 100
})
const validQuantity = computed(() => Number.isSafeInteger(quantity.value) && quantity.value >= 1
  && quantity.value <= quantityMaximum.value)
const personalTotalFen = computed(() => personalProduct.value === 'reviewer_month'
  ? pricing.value?.reviewer_month.price_fen ?? 0
  : (pricing.value?.credit_pack.price_fen ?? 0) * (validQuantity.value ? quantity.value : 0))
const enterpriseTotalFen = computed(() => (selectedEnterprise.value && enterpriseProduct.value === 'enterprise_renew'
  ? pricing.value?.enterprise_seat_month.price_fen ?? 0
  : pricing.value?.credit_pack.price_fen ?? 0) * (validQuantity.value ? quantity.value : 0))
const enterpriseCreateTotalFen = computed(() => (pricing.value?.enterprise_seat_month.price_fen ?? 0)
  * (validQuantity.value ? quantity.value : 0))
const canCreatePersonal = computed(() => Boolean(pricing.value?.sales_enabled && pricing.value.channels[channel.value]
  && !creating.value && validQuantity.value))
const canCreateEnterprise = computed(() => Boolean(pricing.value?.sales_enabled && pricing.value.channels[channel.value]
  && !creating.value && validQuantity.value
  && (selectedEnterprise.value || enterpriseName.value.trim())))
const canCancelActiveOrder = computed(() => Boolean(activeOrder.value && activeOrder.value.status !== 'paid'
  && activeOrder.value.status !== 'refunded' && activeOrder.value.status !== 'cancelled'))

const number = (value: number) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)
const money = (fen: number) => `¥${(fen / 100).toFixed(2)}`
const dateTime = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
const productName = (product: BillingOrder['product']) => ({
  personal_credits: '个人积分充值', reviewer_month: '个人审查员月度会员', enterprise_credits: '企业积分充值',
  enterprise_create: '企业席位首期套餐', enterprise_renew: '企业席位续费',
}[product] || product)
const orderStatus = (order: BillingOrder) => {
  if (order.status === 'paid') return '已支付'
  if (order.status === 'refunded') return '已退款'
  if (order.status === 'cancelled') return '已关闭'
  if (order.payment_status === 'unknown') return '支付结果待确认'
  if (order.payment_status === 'ready') return '等待支付'
  return '处理中'
}
const refundStatus = (status: string) => ({ requested: '待审核', manual_review: '人工审核中', approved: '已批准待渠道处理',
  processing: '渠道处理中', unknown: '退款结果待确认', failed: '退款失败', succeeded: '已退款', rejected: '已拒绝' }[status] || status)

function freshRequestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, item => item.toString(16).padStart(2, '0')).join('')
}
function requestIdFor(input: Record<string, unknown>) {
  const key = `prism:billing-order-request:${currentUserId.value}`
  const fingerprint = JSON.stringify(input)
  const memoryKey = `${currentUserId.value}:${fingerprint}`
  const remembered = pendingOrderKeys.get(memoryKey)
  if (remembered) return remembered
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null') as { id?: string; fingerprint?: string } | null
    if (saved?.id && saved.fingerprint === fingerprint) {
      pendingOrderKeys.set(memoryKey, saved.id)
      return saved.id
    }
    const id = freshRequestId()
    pendingOrderKeys.set(memoryKey, id)
    try { sessionStorage.setItem(key, JSON.stringify({ id, fingerprint })) }
    catch { pendingOrderStorageUnavailable = true }
    return id
  } catch {
    pendingOrderStorageUnavailable = true
    const id = freshRequestId()
    pendingOrderKeys.set(memoryKey, id)
    return id
  }
}
function clearRequestId(input: Record<string, unknown>) {
  pendingOrderKeys.delete(`${currentUserId.value}:${JSON.stringify(input)}`)
  try { sessionStorage.removeItem(`prism:billing-order-request:${currentUserId.value}`) } catch { /* 当前页面仍可继续 */ }
}

async function loadOrders(current = generation) {
  loadingOrders.value = true
  try {
    const result = await getBillingOrders()
    if (current === generation) orders.value = result
  } catch {
    if (current === generation) pageError.value = '订单列表加载失败，请稍后重试。'
  } finally { if (current === generation) loadingOrders.value = false }
}
async function loadEnterpriseOrders(id: number, current = generation) {
  const request = ++enterpriseOrderGeneration, account = currentUserId.value, token = getToken()
  loadingEnterpriseOrders.value = true; enterpriseOrderError.value = ''
  try {
    const result = await getEnterpriseBillingOrders(id)
    if (request === enterpriseOrderGeneration && current === generation && id === selectedEnterpriseId.value
      && account === currentUserId.value && token === getToken()) enterpriseOrders.value = result
  } catch {
    if (request === enterpriseOrderGeneration && current === generation && id === selectedEnterpriseId.value
      && account === currentUserId.value && token === getToken()) {
      enterpriseOrders.value = []; enterpriseOrderError.value = '企业订单加载失败，请检查企业权限后重试。'
    }
  } finally {
    if (request === enterpriseOrderGeneration && current === generation) loadingEnterpriseOrders.value = false
  }
}
function refreshEnterpriseOrders() {
  if (selectedEnterpriseId.value) void loadEnterpriseOrders(selectedEnterpriseId.value)
}
async function load() {
  const current = ++generation, account = currentUserId.value, token = getToken()
  if (!account) return
  loading.value = true; pageError.value = ''; orderError.value = ''
  try {
    const [priceData, walletData, entitlementData, companyData, orderData] = await Promise.all([
      getBillingPricing(), getBillingWallet(), getBillingEntitlement(), listMyEnterprises(), getBillingOrders(),
    ])
    if (current !== generation || account !== currentUserId.value || token !== getToken()) return
    pricing.value = priceData; wallet.value = walletData; entitlement.value = entitlementData
    enterprises.value = companyData; orders.value = orderData
    const requestedId = Number(route.query.enterprise_id)
    const nextEnterpriseId = managedEnterprises.value.some(item => item.id === requestedId)
      ? requestedId : managedEnterprises.value[0]?.id ?? null
    if (selectedEnterpriseId.value !== nextEnterpriseId) selectedEnterpriseId.value = nextEnterpriseId
  } catch {
    if (current === generation && account === currentUserId.value && token === getToken()) {
      pageError.value = '计费信息加载失败，请检查网络后重试。'
    }
  } finally { if (current === generation) loading.value = false }
}
async function createOrder(input: Omit<Parameters<typeof createBillingOrder>[0], 'client_request_id'>) {
  if (!pricing.value?.sales_enabled || creating.value) return
  const orderRequest = ++orderGeneration
  creating.value = true; orderError.value = ''; activeRefund.value = null
  const account = currentUserId.value, token = getToken(), current = generation
  const fingerprint = { ...input }
  try {
    const order = await createBillingOrder({ ...input, client_request_id: requestIdFor(fingerprint) })
    if (current !== generation || account !== currentUserId.value || token !== getToken()
      || orderRequest !== orderGeneration) return
    clearRequestId(fingerprint)
    await setActiveOrder(order, orderRequest)
    await loadOrders(current)
    if (input.enterprise_id) await loadEnterpriseOrders(input.enterprise_id, current)
  } catch {
    if (current === generation && account === currentUserId.value && token === getToken()
      && orderRequest === orderGeneration) {
      orderError.value = pendingOrderStorageUnavailable
        ? '订单结果暂未确认。本页面相同购买会复用请求标识；刷新页面后请先查询订单，避免重复购买。'
        : '订单结果暂未确认。刷新订单列表并查询原请求，不要为同一购买重复新建订单。'
      await loadOrders(current)
    }
  } finally { if (current === generation) creating.value = false }
}
function createPersonalOrder() {
  if (personalProduct.value === 'reviewer_month') {
    return void createOrder({ product: 'reviewer_month', quantity: 1, channel: channel.value })
  }
  if (!validQuantity.value) return
  return void createOrder({ product: 'personal_credits', quantity: quantity.value, channel: channel.value })
}
function createEnterpriseOrder() {
  if (!validQuantity.value) return
  if (selectedEnterprise.value) {
    return void createOrder({ product: enterpriseProduct.value, quantity: quantity.value, channel: channel.value,
      enterprise_id: selectedEnterprise.value.id })
  }
  if (!enterpriseName.value.trim()) return
  return void createOrder({ product: 'enterprise_create', quantity: quantity.value, channel: channel.value,
    enterprise_name: enterpriseName.value.trim() })
}
async function setActiveOrder(order: BillingOrder, orderRequest: number) {
  const current = generation, account = currentUserId.value, token = getToken()
  let nextQr = '', nextQrError = false
  if (order.payment_code_url && order.status !== 'paid' && order.status !== 'refunded') {
    try { nextQr = await QRCode.toDataURL(order.payment_code_url, { width: 240, margin: 2, errorCorrectionLevel: 'M' }) }
    catch { nextQrError = true }
  }
  if (current !== generation || account !== currentUserId.value || token !== getToken()
    || orderRequest !== orderGeneration) return
  activeRefund.value = null
  activeOrder.value = order; qrDataUrl.value = nextQr; qrError.value = nextQrError; orderError.value = ''
  if (order.status === 'paid') {
    const refund = await getBillingRefund(order.order_no).catch(() => null)
    if (current === generation && account === currentUserId.value && token === getToken()
      && orderRequest === orderGeneration && activeOrder.value?.order_no === order.order_no) activeRefund.value = refund
  }
}
async function selectOrder(orderNo: string) {
  const current = ++orderGeneration
  orderLoading.value = true; orderError.value = ''
  try {
    const order = await getBillingOrder(orderNo)
    if (current === orderGeneration) await setActiveOrder(order, current)
  } catch { if (current === orderGeneration) orderError.value = '订单详情加载失败。' }
  finally { if (current === orderGeneration) orderLoading.value = false }
}
async function refreshActiveOrder() {
  const old = activeOrder.value
  if (!old || orderLoading.value) return
  const current = ++orderGeneration
  const account = currentUserId.value, token = getToken(), accountGeneration = generation
  const isCurrent = () => current === orderGeneration && accountGeneration === generation
    && account === currentUserId.value && token === getToken()
  orderLoading.value = true; orderError.value = ''
  try {
    const order = await refreshBillingOrder(old.order_no)
    if (current !== orderGeneration) return
    await setActiveOrder(order, current)
    if (!isCurrent()) return
    if (order.status === 'paid' && (order.product === 'enterprise_create' || order.product === 'enterprise_renew')) {
      await load()
    } else if (order.status === 'paid' && order.product === 'enterprise_credits' && order.enterprise_id) {
      const nextWallet = await getEnterpriseBillingWallet(order.enterprise_id).catch(() => null)
      if (!isCurrent() || selectedEnterpriseId.value !== order.enterprise_id) return
      enterpriseWallet.value = nextWallet
      await loadEnterpriseOrders(order.enterprise_id)
    } else {
      await loadOrders(generation)
    }
    if (order.status === 'paid' && order.product !== 'enterprise_credits'
      && order.product !== 'enterprise_create' && order.product !== 'enterprise_renew') {
      const nextWallet = await getBillingWallet().catch(() => null)
      if (!isCurrent()) return
      if (nextWallet) wallet.value = nextWallet
      const nextEntitlement = await getBillingEntitlement().catch(() => null)
      if (!isCurrent()) return
      if (nextEntitlement) entitlement.value = nextEntitlement
    }
  } catch {
    if (current === orderGeneration) orderError.value = '支付结果暂时无法确认；权益不会提前发放，请稍后再查。'
  } finally { if (current === orderGeneration) orderLoading.value = false }
}
async function cancelActiveOrder() {
  const order = activeOrder.value
  if (!order || orderLoading.value) return
  const current = generation, account = currentUserId.value, token = getToken()
  const orderRequest = ++orderGeneration
  orderLoading.value = true; orderError.value = ''
  try {
    const result = await cancelBillingOrder(order.order_no)
    if (current !== generation || account !== currentUserId.value || token !== getToken()
      || orderRequest !== orderGeneration) return
    await setActiveOrder(result, orderRequest)
    await loadOrders(current)
    if (result.enterprise_id) await loadEnterpriseOrders(result.enterprise_id, current)
  } catch { if (current === generation && orderRequest === orderGeneration) orderError.value = '关闭结果尚未确认，请刷新订单状态。' }
  finally { if (current === generation && orderRequest === orderGeneration) orderLoading.value = false }
}
async function submitRefund() {
  const order = activeOrder.value, reason = refundReason.value.trim()
  if (!order || order.status !== 'paid' || !reason || orderLoading.value) return
  const current = generation, account = currentUserId.value, token = getToken()
  const orderRequest = ++orderGeneration
  orderLoading.value = true; orderError.value = ''
  try {
    const result = await requestBillingRefund(order.order_no, reason)
    if (current !== generation || account !== currentUserId.value || token !== getToken()
      || orderRequest !== orderGeneration || activeOrder.value?.order_no !== order.order_no) return
    activeRefund.value = result
    refundReason.value = ''
  } catch { if (current === generation && orderRequest === orderGeneration) orderError.value = '退款申请未确认，请重新查询订单退款状态。' }
  finally { if (current === generation && orderRequest === orderGeneration) orderLoading.value = false }
}
async function copyPaymentCode() {
  const value = activeOrder.value?.payment_code_url
  if (!value) return
  try { await navigator.clipboard.writeText(value); orderError.value = '支付链接已复制。' }
  catch { orderError.value = '无法访问剪贴板，请使用页面中的支付链接。' }
}
watch(selectedEnterpriseId, async id => {
  const request = ++enterpriseWalletGeneration, current = generation
  const account = currentUserId.value, token = getToken()
  enterpriseWallet.value = null
  enterpriseOrderGeneration++
  enterpriseOrders.value = []; enterpriseOrderError.value = ''; loadingEnterpriseOrders.value = false
  if (activeOrder.value?.enterprise_id && activeOrder.value.enterprise_id !== id) {
    orderGeneration++; orderLoading.value = false
    activeOrder.value = null; activeRefund.value = null; qrDataUrl.value = ''
  }
  if (!id) { enterpriseWallet.value = null; return }
  const result = await getEnterpriseBillingWallet(id).catch(() => null)
  if (request === enterpriseWalletGeneration && current === generation && id === selectedEnterpriseId.value
    && account === currentUserId.value && token === getToken()) enterpriseWallet.value = result
  if (request === enterpriseWalletGeneration && current === generation && id === selectedEnterpriseId.value
    && account === currentUserId.value && token === getToken()) await loadEnterpriseOrders(id, current)
})
watch([currentUserId, () => user.token], () => {
  generation++; orderGeneration++; enterpriseWalletGeneration++; enterpriseOrderGeneration++
  pricing.value = null; wallet.value = null; entitlement.value = null; enterprises.value = []; enterpriseWallet.value = null
  orders.value = []; enterpriseOrders.value = []; activeOrder.value = null; activeRefund.value = null; qrDataUrl.value = ''
  loadingEnterpriseOrders.value = false; enterpriseOrderError.value = ''
  pageError.value = ''; orderError.value = ''; loading.value = false; loadingOrders.value = false; creating.value = false
  orderLoading.value = false
  purchaseScope.value = 'personal'; personalProduct.value = 'personal_credits'; enterpriseProduct.value = 'enterprise_renew'
  selectedEnterpriseId.value = null; enterpriseName.value = ''; quantity.value = 1; channel.value = 'alipay'; refundReason.value = ''
  if (currentUserId.value) void load()
}, { flush: 'sync' })
onMounted(() => { if (currentUserId.value) void load() })
onBeforeUnmount(() => { generation++; orderGeneration++; enterpriseOrderGeneration++ })
</script>

<style scoped>
.billing-page { max-width: 1180px; margin: 0 auto; padding: 26px; color: #20243a; }
.page-head, .section-head, .tabs, .channels { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-head { margin-bottom: 18px; } h1 { margin: 0; font-size: 24px; } h2 { margin: 0 0 14px; font-size: 17px; }
.page-head p, .muted, small { color: #69718a; font-size: 13px; line-height: 1.55; }
.gate-note, .error-note, .notice { padding: 13px 15px; border-radius: 10px; line-height: 1.55; }
.gate-note { color: #805213; background: #fff6e7; border: 1px solid #f1d5a6; }
.error-note { color: #9f3333; background: #fff0f0; }.notice { background: white; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 18px 0; }
.summary-card, .card { background: #fff; border: 1px solid #e9eaf3; border-radius: 14px; padding: 18px; }
.summary-card span, .summary-card small { display: block; }.summary-card span { color: #69718a; font-size: 13px; }
.summary-card strong { display: block; margin: 8px 0; font-size: 24px; }.summary-card small { font-size: 12px; }
.purchase { max-width: 760px; }.tabs { justify-content: flex-start; margin-bottom: 18px; border-bottom: 1px solid #eceef5; }
.tabs button[aria-selected='true'] { color: #424cc5; border-color: #7379ec; }
.field { display: grid; gap: 7px; margin: 14px 0; font-size: 13px; }.field span { font-weight: 600; }
input, select, textarea, button { font: inherit; border: 1px solid #d9ddeb; background: white; border-radius: 8px; padding: 9px 11px; color: inherit; }
input, select, textarea { width: 100%; }.field small { font-weight: 400; }.muted { margin: 10px 0; }
.price { margin: 18px 0 12px; }.price strong { font-size: 22px; color: #282f84; }
.channels { justify-content: flex-start; margin-bottom: 14px; }.channels button[aria-pressed='true'] { border-color: #5962d6; color: #424cc5; background: #f0f1ff; }
button { cursor: pointer; } button:disabled { opacity: .55; cursor: not-allowed; }.primary { min-width: 180px; color: white; background: #4f58ce; border-color: #4f58ce; }
.order-panel, .orders-panel { margin-top: 16px; }.section-head h2 { margin: 0; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 8px 16px; }.section-head + dl { margin-top: 14px; }
dt { color: #69718a; }dd { margin: 0; overflow-wrap: anywhere; }
.qr-area { display: grid; justify-items: start; gap: 8px; margin: 14px 0; }.qr-area img { width: 240px; max-width: 100%; height: auto; image-rendering: pixelated; }
.qr-area p { max-width: 540px; color: #69718a; font-size: 13px; line-height: 1.5; }.refund-form { margin-top: 18px; padding-top: 14px; border-top: 1px solid #eceef5; }
.refund-status { margin-top: 15px; padding: 12px; background: #f6f7fb; border-radius: 9px; }.refund-status p { color: #69718a; font-size: 13px; }
.table-wrap { overflow-x: auto; margin-top: 12px; }table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #eef0f5; white-space: nowrap; }th { color: #69718a; }
@media (max-width: 680px) { .billing-page { padding: 16px; }.page-head { align-items: flex-start; }.summary-grid { grid-template-columns: 1fr; }.purchase, .order-panel, .orders-panel { padding: 14px; }.tabs, .channels { flex-wrap: wrap; }dl { grid-template-columns: 1fr; gap: 3px; }dd { margin-bottom: 8px; }.table-wrap { margin-inline: -5px; } }
</style>
