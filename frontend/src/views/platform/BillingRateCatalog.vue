<template>
  <main class="rate-catalog-page" :aria-busy="loading || busy">
    <header class="page-head">
      <div>
        <p class="eyebrow">平台财务工作台</p>
        <h1>计费费率目录</h1>
        <p>安排模型 Token 与企业小菱 Search/Fetch 的积分扣费和供应商成本；每个版本需由另一位获授权的费率复核人审批。</p>
      </div>
      <button type="button" :disabled="loading || busy" @click="load">刷新目录</button>
    </header>

    <BillingC4ReviewerManagement kind="rate" />

    <section class="boundary-note" aria-label="费率治理边界">
      <strong>{{ currentPricing ? (currentPricing.sales_enabled ? '开售已启用' : '开售锁仍关闭') : '开售状态读取中' }}</strong>
      <span>这里管理模型 Token 与企业小菱 Search/Fetch 的积分费率。变化只影响生效后新建的预占，已有预占继续按原快照结算。</span>
      <span>30% 是模型和按次工具的直采最低毛利线。它不包含支付费率、税费、退款、服务器或滥用成本，不能当作整体盈利证明。供应商与汇率价格由管理员按官方来源核验并录入；本页不会自动抓取价格。</span>
    </section>

    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>

    <section class="panel current-panel" aria-labelledby="current-title">
      <div class="section-head">
        <div><h2 id="current-title">当前对外费率</h2><p>普通用户报价与新预占使用同一份已生效目录。</p></div>
        <span class="version-chip">{{ currentPricing?.pricing_version?.slice(0, 12) || '读取中' }}</span>
      </div>
      <div v-if="loading && !currentPricing" class="empty" role="status">正在读取生效目录…</div>
      <div v-else-if="currentPricing" class="current-grid">
        <div><span>生效时间</span><strong>{{ dateTime(currentPricing.pricing_effective_at) }}</strong></div>
        <div><span>积分包参考收入</span><strong>{{ money(currentPricing.credit_pack.price_fen) }} / {{ currentPricing.credit_pack.credits.toLocaleString() }} 积分</strong></div>
        <div><span>下一次计划切换</span><strong>{{ currentPricing.next_pricing_effective_at ? dateTime(currentPricing.next_pricing_effective_at) : '无' }}</strong></div>
        <p v-if="currentPricing.model_pricing_ready === false" role="status" class="alert">
          模型价格来源未完成核验或已过期，当前仅供查看；新模型计费预占已暂停，请创建并审批有效目录。
        </p>
        <div v-for="model in currentPricing.billable_models" :key="model" class="current-model">
          <span>{{ model }}</span><strong>输入 {{ currentPricing.model_milli_per_token[model]?.input }} · 输出 {{ currentPricing.model_milli_per_token[model]?.output }} 毫积分 / Token</strong>
        </div>
        <div v-for="tool in toolNames" :key="tool.key" class="current-model">
          <span>企业小菱 {{ tool.label }}</span>
          <strong>{{ currentPricing.tool_milli_credits_per_success?.[tool.key]
            ? `${currentPricing.tool_milli_credits_per_success[tool.key]} 毫积分 / 成功请求`
            : '未开通' }}</strong>
        </div>
      </div>
    </section>

    <section class="panel draft-panel" aria-labelledby="draft-title">
      <div class="section-head">
        <div><h2 id="draft-title">创建费率草稿</h2><p>草稿需覆盖当前全部可计费模型；来源摘要由服务器根据本次提交的价格事实生成。</p></div>
      </div>
      <form class="catalog-form" @submit.prevent="createDraft">
        <div class="form-grid">
          <label class="field"><span>计划生效时间（本地时区）</span><input v-model="form.effectiveLocal" type="datetime-local" required :disabled="busy" /></label>
          <label class="field"><span>模型美元官方价格页面</span><input v-model.trim="form.sourceUrl" type="url" maxlength="512" required placeholder="https://api-docs.deepseek.com/quick_start/pricing/" :disabled="busy" /></label>
          <label class="field"><span>模型美元价格核验时间（本地时区）</span><input v-model="form.checkedLocal" type="datetime-local" required :disabled="busy" /></label>
          <label class="field"><span>模型人民币官方价格页面</span><input v-model.trim="form.modelCnySourceUrl" type="url" maxlength="512" required aria-label="模型人民币价格来源" placeholder="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" :disabled="busy" /></label>
          <label class="field"><span>模型人民币价格核验时间（本地时区）</span><input v-model="form.modelCnyCheckedLocal" type="datetime-local" required aria-label="模型人民币价格核验时间" :disabled="busy" /><small>请在核验官方人民币价目后填写实际核验时间。</small></label>
          <label class="field"><span>人民币 / 美元参考汇率</span><input v-model.number="form.fxCnyPerUsd" type="number" min="0.0001" max="100000" step="0.0001" required :disabled="busy" /><small>使用已核验的中行折算参考价，按 4 位小数存档。</small></label>
          <label class="field"><span>中国银行外汇牌价页面</span><input v-model.trim="form.fxSourceUrl" type="url" maxlength="512" required placeholder="https://www.boc.cn/sourcedb/whpj/index.html" :disabled="busy" /></label>
          <label class="field"><span>汇率核验时间（本地时区）</span><input v-model="form.fxCheckedLocal" type="datetime-local" required :disabled="busy" /></label>
        </div>
        <p class="form-help financial-basis">毛利计算按当前标准积分商品固定基准：¥4.00 / 1000 积分，与下单价格和到账数量一致。本阶段此项只读。</p>

        <label class="field change-note"><span>变更说明</span><textarea v-model.trim="form.changeNote" maxlength="500" rows="2" required placeholder="例如：供应商峰值价格调整，按官方价目更新成本并同步调高积分费率。" :disabled="busy" /></label>

        <div class="table-wrap rate-items">
          <table>
            <thead><tr><th>可计费模型</th><th>用户输入费率<br /><small>毫积分 / Token</small></th><th>用户输出费率<br /><small>毫积分 / Token</small></th><th>供应商输入价<br /><small>美元 / 百万 Token</small></th><th>供应商输出价<br /><small>美元 / 百万 Token</small></th><th>供应商输入价<br /><small>人民币微元 / 百万 Token</small></th><th>供应商输出价<br /><small>人民币微元 / 百万 Token</small></th></tr></thead>
            <tbody>
              <tr v-for="item in form.items" :key="item.model_name">
                <th scope="row">{{ item.model_name }}</th>
                <td><div class="fraction"><input v-model.number="item.input_rate_numerator" type="number" min="1" step="1" :aria-label="`${item.model_name} 用户输入费率分子`" :disabled="busy" /><span>/</span><input v-model.number="item.input_rate_denominator" type="number" min="1" step="1" :aria-label="`${item.model_name} 用户输入费率分母`" :disabled="busy" /></div></td>
                <td><div class="fraction"><input v-model.number="item.output_rate_numerator" type="number" min="1" step="1" :aria-label="`${item.model_name} 用户输出费率分子`" :disabled="busy" /><span>/</span><input v-model.number="item.output_rate_denominator" type="number" min="1" step="1" :aria-label="`${item.model_name} 用户输出费率分母`" :disabled="busy" /></div></td>
                <td><label class="money-input"><span>$</span><input v-model.number="item.provider_input_usd_micros_per_million" type="number" min="0" step="1" :aria-label="`${item.model_name} 供应商输入价格，微美元每百万 Token`" :disabled="busy" /></label></td>
                <td><label class="money-input"><span>$</span><input v-model.number="item.provider_output_usd_micros_per_million" type="number" min="0" step="1" :aria-label="`${item.model_name} 供应商输出价格，微美元每百万 Token`" :disabled="busy" /></label></td>
                <td><label class="money-input"><span>¥</span><input v-model.number="item.provider_input_cny_micros_per_million" type="number" min="1" step="1" required :aria-label="`${item.model_name} 供应商输入价格，人民币微元每百万 Token`" :disabled="busy" /></label></td>
                <td><label class="money-input"><span>¥</span><input v-model.number="item.provider_output_cny_micros_per_million" type="number" min="1" step="1" required :aria-label="`${item.model_name} 供应商输出价格，人民币微元每百万 Token`" :disabled="busy" /></label></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="form-help">供应商美元价格以微美元计：300000 表示每百万 Token 0.30 美元；人民币价格以微元计：2000000 表示 2 元。请按实际核验的高峰、缓存未命中价填写。</p>
        <section class="tool-rate-editor" aria-labelledby="tool-rate-title">
          <div class="tool-rate-heading"><div><h3 id="tool-rate-title">企业小菱 Search / Fetch 按次费率</h3><p>费用按成功请求次数结算。成本或售价保持 0 时该工具关闭；启用时两者都必须大于 0。</p></div><span :class="['status-pill', toolRatesEnabled ? 'active' : 'canceled']">{{ toolRatesEnabled ? '草稿将启用' : '保持关闭' }}</span></div>
          <div class="form-grid">
            <label class="field"><span>Search / Fetch 中文人民币价目来源</span><input v-model.trim="form.toolCnySourceUrl" type="url" maxlength="512" aria-label="Search/Fetch 中文人民币价格来源" placeholder="https://api-docs.deepseek.com/zh-cn/guides/search/" :required="toolRatesEnabled" :disabled="busy" /></label>
            <label class="field"><span>中文价格核验时间（本地时区）</span><input v-model="form.toolCnyCheckedLocal" type="datetime-local" aria-label="Search/Fetch 人民币价格核验时间" :required="toolRatesEnabled" :disabled="busy" /></label>
            <label class="field"><span>Search / Fetch 英文美元价目来源</span><input v-model.trim="form.toolUsdSourceUrl" type="url" maxlength="512" aria-label="Search/Fetch 英文美元价格来源" placeholder="https://api-docs.deepseek.com/quick_start/pricing/" :required="toolRatesEnabled" :disabled="busy" /></label>
            <label class="field"><span>英文价格核验时间（本地时区）</span><input v-model="form.toolUsdCheckedLocal" type="datetime-local" aria-label="Search/Fetch 美元价格核验时间" :required="toolRatesEnabled" :disabled="busy" /></label>
          </div>
          <div class="tool-rate-grid">
            <fieldset v-for="tool in toolNames" :key="tool.key" class="tool-rate-card">
              <legend>{{ tool.label }}</legend>
              <label class="field"><span>供应商成本（折算后人民币微元 / 成功请求）</span><input v-model.number="form[tool.costField]" type="number" min="0" step="1" :aria-label="`${tool.label} 供应商成本，人民币微元每成功请求`" :disabled="busy" /><small>按已核验汇率折算外币报价并向上取整；低于当前官方双币报价的输入会被拒绝。</small></label>
              <label class="field"><span>用户收费（毫积分 / 成功请求）</span><input v-model.number="form[tool.priceField]" type="number" min="0" step="1" :aria-label="`${tool.label} 用户费率，毫积分每成功请求`" :disabled="busy" /><small>按上方积分包收入基准核算至少 30% 直采毛利。</small></label>
            </fieldset>
          </div>
        </section>
        <div class="form-actions"><button class="primary" type="submit" :disabled="busy || loading || form.items.length === 0">{{ busy ? '正在提交…' : '创建待审批草稿' }}</button></div>
      </form>
    </section>

    <section class="catalog-section" aria-labelledby="history-title">
      <div class="section-head"><div><h2 id="history-title">版本与审批记录</h2><p>目录及供应商核价快照按版本保留；拒绝和取消也会追加事件记录。</p></div><span class="count">{{ catalogs.length }} 个版本</span></div>
      <p v-if="loading && catalogs.length === 0" class="panel empty" role="status">正在读取费率版本…</p>
      <p v-else-if="catalogs.length === 0" class="panel empty">尚无费率目录。</p>
      <article v-for="catalog in catalogs" :key="catalog.id" class="panel catalog-card">
        <header class="catalog-head">
          <div><div class="catalog-title"><span :class="['status-pill', statusClass(catalog)]">{{ statusLabel(catalog) }}</span><code>{{ catalog.version_key }}</code></div><h3>{{ dateTime(catalog.effective_at) }}</h3><p>{{ catalog.change_note }}</p></div>
          <span class="source-age">来源核验 {{ dateTime(catalog.source_checked_at) }}</span>
        </header>
        <dl class="catalog-facts">
          <div><dt>模型美元价格来源</dt><dd><a :href="catalog.source_url" target="_blank" rel="noopener noreferrer">{{ catalog.source_url }}</a><small>核验 {{ dateTime(catalog.source_checked_at) }}</small></dd></div>
          <div><dt>模型人民币价格来源</dt><dd><template v-if="catalog.model_cny_source_url && catalog.model_cny_source_checked_at"><a :href="catalog.model_cny_source_url" target="_blank" rel="noopener noreferrer">{{ catalog.model_cny_source_url }}</a><small>核验 {{ dateTime(catalog.model_cny_source_checked_at) }}</small></template><span v-else>未核验</span></dd></div>
          <div><dt>工具人民币价格来源</dt><dd><template v-if="catalog.tool_cny_source_url && catalog.tool_cny_source_checked_at"><a :href="catalog.tool_cny_source_url" target="_blank" rel="noopener noreferrer">{{ catalog.tool_cny_source_url }}</a><small>核验 {{ dateTime(catalog.tool_cny_source_checked_at) }}</small></template><span v-else>未启用</span></dd></div>
          <div><dt>工具美元价格来源</dt><dd><template v-if="catalog.tool_usd_source_url && catalog.tool_usd_source_checked_at"><a :href="catalog.tool_usd_source_url" target="_blank" rel="noopener noreferrer">{{ catalog.tool_usd_source_url }}</a><small>核验 {{ dateTime(catalog.tool_usd_source_checked_at) }}</small></template><span v-else>未启用</span></dd></div>
          <div><dt>汇率来源</dt><dd><a :href="catalog.fx_source_url" target="_blank" rel="noopener noreferrer">{{ catalog.fx_source_url }}</a><small>核验 {{ dateTime(catalog.fx_source_checked_at) }}</small></dd></div>
          <div><dt>价格事实摘要</dt><dd><code>{{ catalog.source_sha256 }}</code></dd></div>
          <div><dt>汇率快照</dt><dd>¥{{ catalog.fx_cny_per_usd.toFixed(4) }} / USD</dd></div>
          <div><dt>积分收入基准</dt><dd>{{ money(catalog.credit_pack_price_fen) }} / {{ catalog.credit_pack_credits.toLocaleString() }} 积分</dd></div>
          <div><dt>创建 / 审批</dt><dd>#{{ catalog.created_by ?? '迁移基线' }} / {{ catalog.approved_by ?? '—' }}</dd></div>
        </dl>
        <div class="margin-grid" aria-label="Token 直采毛利估算">
          <div v-for="item in catalog.items" :key="item.model_name" class="margin-item">
            <strong>{{ item.model_name }}</strong>
            <span v-if="catalog.model_cny_source_url && catalog.model_cny_source_checked_at && item.provider_input_cny_micros_per_million != null && item.provider_output_cny_micros_per_million != null">输入 {{ margin(catalog.margin_bps[item.model_name]?.input_bps) }} · 输出 {{ margin(catalog.margin_bps[item.model_name]?.output_bps) }}<br />人民币输入 ¥{{ (item.provider_input_cny_micros_per_million / 1_000_000).toFixed(2) }} · 输出 ¥{{ (item.provider_output_cny_micros_per_million / 1_000_000).toFixed(2) }} / 百万 Token</span>
            <span v-else>人民币价格未核验，无法确认双币毛利</span>
          </div>
        </div>
        <div class="tool-margin-grid" aria-label="Search 与 Fetch 直采毛利估算">
          <div v-for="tool in toolNames" :key="tool.key" class="margin-item"><strong>{{ tool.label }}</strong><span v-if="catalog.tool_margin_bps[tool.key] !== null">成本 ¥{{ (catalog[tool.catalogCostField] / 1_000_000).toFixed(6) }} · 用户价 {{ catalog[tool.catalogPriceField] }} 毫积分 · 毛利 {{ margin(catalog.tool_margin_bps[tool.key]) }}</span><span v-else>未启用</span></div>
        </div>
        <p v-if="catalog.supplier_price_diff.increases.length" class="alert cost-alert" role="status">相对前一批准版本，检测到 {{ catalog.supplier_price_diff.increases.length }} 项供应商价格上涨；请核对调价是否同步保障 30% 毛利线。</p>
        <p v-if="catalog.tool_supplier_price_diff.increases.length" class="alert cost-alert" role="status">相对前一批准版本，检测到 {{ catalog.tool_supplier_price_diff.increases.length }} 项 Search/Fetch 供应商成本上涨；请核对工具单价是否仍保障 30% 毛利线。</p>
        <p v-else-if="catalog.supplier_price_diff.decreases.length" class="alert">相对前一批准版本，检测到 {{ catalog.supplier_price_diff.decreases.length }} 项供应商价格下降。</p>
        <p v-else class="muted">该版本没有高于或低于前一批准版本的供应商成本。</p>
        <div v-if="catalog.status === 'draft'" class="decision-box">
          <p v-if="catalog.created_by === currentUserId" class="hint warning">你是草稿创建人，不能审批本人版本；请另一位获授权的费率复核人审批。</p>
          <p v-else class="hint">该版本将在 {{ dateTime(catalog.effective_at) }} 生效；审批后会自动按 UTC 切换。</p>
          <label v-if="catalog.created_by !== currentUserId" class="field decision-note"><span>拒绝原因（仅拒绝时使用）</span><input v-model.trim="decisionNotes[catalog.id]" maxlength="500" placeholder="说明价格事实或毛利校验问题" :disabled="busy" /></label>
          <div class="actions"><button v-if="catalog.created_by !== currentUserId" type="button" class="danger" :disabled="busy || !decisionNotes[catalog.id]?.trim()" @click="decide(catalog, 'reject')">拒绝草稿</button><button v-if="catalog.created_by !== currentUserId" type="button" class="primary" :disabled="busy" @click="decide(catalog, 'approve')">批准计划费率</button></div>
        </div>
        <div v-else-if="catalog.status === 'approved' && isFuture(catalog)" class="decision-box">
          <label class="field decision-note"><span>取消申请原因</span><input v-model.trim="decisionNotes[catalog.id]" :data-cancel-note="catalog.id" maxlength="500" :disabled="busy" /></label>
          <button class="danger" type="button" :data-cancel-request="catalog.id" :disabled="busy || !decisionNotes[catalog.id]?.trim()" @click="requestCancellation(catalog)">申请取消</button>
        </div>
        <section class="events" :aria-label="`版本 ${catalog.id} 的审批事件`"><h4>审计事件</h4><ol><li v-for="(event, index) in catalog.events" :key="`${catalog.id}-${index}`"><strong>{{ eventLabel(event.action) }}</strong><time>{{ dateTime(event.created_at) }}</time><span>管理员 #{{ event.actor_id ?? '迁移' }} · {{ event.note }}</span></li></ol></section>
      </article>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import BillingC4ReviewerManagement from './BillingC4ReviewerManagement.vue'
import { getToken } from '@/utils/token'
import {
  approveBillingRateCatalog, createBillingRateCatalog, proposeBillingRateCancellation,
  getBillingPricing, listBillingRateCatalogs, rejectBillingRateCatalog,
  type BillingPricing, type BillingRateCatalog, type BillingRateCatalogItem,
} from '@/api/billing'

const user = useUserStore()
const currentUserId = computed(() => Number(user.profile?.id ?? 0))
const currentPricing = ref<BillingPricing | null>(null)
const catalogs = ref<BillingRateCatalog[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const decisionNotes = reactive<Record<number, string>>({})
let generation = 0
let alive = true

const toolNames = [
  { key: 'search', label: 'Search', costField: 'toolSearchCost', priceField: 'toolSearchPrice',
    catalogCostField: 'tool_search_cost_cny_micros_per_success',
    catalogPriceField: 'tool_search_price_milli_per_success' },
  { key: 'fetch', label: 'Fetch', costField: 'toolFetchCost', priceField: 'toolFetchPrice',
    catalogCostField: 'tool_fetch_cost_cny_micros_per_success',
    catalogPriceField: 'tool_fetch_price_milli_per_success' },
] as const

function localInput(date: Date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}
const form = reactive({
  effectiveLocal: localInput(new Date(Date.now() + 24 * 60 * 60_000)),
  checkedLocal: '',
  modelCnyCheckedLocal: '',
  fxCheckedLocal: '',
  sourceUrl: 'https://api-docs.deepseek.com/quick_start/pricing/',
  modelCnySourceUrl: 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
  fxSourceUrl: 'https://www.boc.cn/sourcedb/whpj/index.html',
  fxCnyPerUsd: 6.7168,
  changeNote: '',
  toolCnySourceUrl: '',
  toolCnyCheckedLocal: '',
  toolUsdSourceUrl: '',
  toolUsdCheckedLocal: '',
  toolSearchCost: 0,
  toolSearchPrice: 0,
  toolFetchCost: 0,
  toolFetchPrice: 0,
  items: [] as BillingRateCatalogItem[],
})
const toolRatesEnabled = computed(() => toolNames.some(tool => form[tool.costField] > 0 || form[tool.priceField] > 0))

function sessionCurrent(request: number, token: string, userId: number) {
  return alive && request === generation && token === getToken() && userId === currentUserId.value
}
function dateTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
function money(fen: number) { return `¥${(fen / 100).toFixed(2)}` }
function margin(value?: number | null) { return typeof value === 'number' ? `${(value / 100).toFixed(2)}%` : '—' }
function isFuture(catalog: BillingRateCatalog) { return new Date(catalog.effective_at).getTime() > Date.now() }
function statusLabel(catalog: BillingRateCatalog) {
  if (catalog.status === 'approved' && !isFuture(catalog)) return '已生效'
  return ({ draft: '待另一人审批', approved: '已批准待生效', rejected: '已拒绝', canceled: '已取消' } as Record<string, string>)[catalog.status] || catalog.status
}
function statusClass(catalog: BillingRateCatalog) {
  return catalog.status === 'approved' && !isFuture(catalog) ? 'active' : catalog.status
}
function eventLabel(action: string) {
  return ({ created: '草稿创建', approved: '版本批准', rejected: '草稿拒绝', canceled: '计划取消' } as Record<string, string>)[action] || action
}
function errorMessage(value: unknown) {
  if (value instanceof Error && value.message) return value.message
  if (typeof value === 'string') return value
  return '请求失败，请刷新后重试。'
}
function defaultItems(): BillingRateCatalogItem[] {
  const models = currentPricing.value?.billable_models ?? [
    'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp',
  ]
  const prior = catalogs.value.find(item => item.status === 'approved')
  return models.map(model => {
    const old = prior?.items.find(item => item.model_name === model)
    return old ? { ...old } : {
      model_name: model, input_rate_numerator: 1, input_rate_denominator: 1,
      output_rate_numerator: 4, output_rate_denominator: 1,
      provider_input_usd_micros_per_million: 300000,
      provider_output_usd_micros_per_million: 1200000,
      provider_input_cny_micros_per_million: null,
      provider_output_cny_micros_per_million: null,
    }
  })
}

async function load() {
  const request = ++generation
  const token = getToken(), userId = currentUserId.value
  if (!token || !userId) return
  loading.value = true; error.value = ''; notice.value = ''
  try {
    const [pricing, result] = await Promise.all([getBillingPricing(), listBillingRateCatalogs(token)])
    if (!sessionCurrent(request, token, userId)) return
    currentPricing.value = pricing
    catalogs.value = result.items
    if (!form.changeNote) {
      const prior = result.items.find(item => item.status === 'approved')
      form.sourceUrl = prior?.source_url || 'https://api-docs.deepseek.com/quick_start/pricing/'
      form.checkedLocal = prior?.source_checked_at ? localInput(new Date(prior.source_checked_at)) : ''
      form.fxSourceUrl = prior?.fx_source_url || 'https://www.boc.cn/sourcedb/whpj/index.html'
      form.fxCheckedLocal = prior?.fx_source_checked_at ? localInput(new Date(prior.fx_source_checked_at)) : ''
      form.modelCnySourceUrl = prior?.model_cny_source_url || 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
      form.modelCnyCheckedLocal = prior?.model_cny_source_checked_at
        ? localInput(new Date(prior.model_cny_source_checked_at)) : ''
      form.fxCnyPerUsd = prior?.fx_cny_per_usd ?? form.fxCnyPerUsd
      form.toolCnySourceUrl = prior?.tool_cny_source_url ?? ''
      form.toolCnyCheckedLocal = prior?.tool_cny_source_checked_at
        ? localInput(new Date(prior.tool_cny_source_checked_at)) : ''
      form.toolUsdSourceUrl = prior?.tool_usd_source_url ?? ''
      form.toolUsdCheckedLocal = prior?.tool_usd_source_checked_at
        ? localInput(new Date(prior.tool_usd_source_checked_at)) : ''
      form.toolSearchCost = prior?.tool_search_cost_cny_micros_per_success ?? 0
      form.toolSearchPrice = prior?.tool_search_price_milli_per_success ?? 0
      form.toolFetchCost = prior?.tool_fetch_cost_cny_micros_per_success ?? 0
      form.toolFetchPrice = prior?.tool_fetch_price_milli_per_success ?? 0
      form.items = defaultItems()
    }
  } catch (cause) {
    if (sessionCurrent(request, token, userId)) error.value = errorMessage(cause)
  } finally {
    if (sessionCurrent(request, token, userId)) loading.value = false
  }
}

async function createDraft() {
  const token = getToken(), userId = currentUserId.value
  if (!token || !userId) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const effective = new Date(form.effectiveLocal), checked = new Date(form.checkedLocal)
    const modelCnyChecked = new Date(form.modelCnyCheckedLocal)
    const fxChecked = new Date(form.fxCheckedLocal)
    const toolCnyChecked = new Date(form.toolCnyCheckedLocal)
    const toolUsdChecked = new Date(form.toolUsdCheckedLocal)
    if (!Number.isFinite(effective.getTime()) || !Number.isFinite(checked.getTime())
      || !Number.isFinite(fxChecked.getTime()) || !Number.isFinite(modelCnyChecked.getTime())) {
      throw new Error('请填写有效的生效时间、模型人民币与美元价格核验时间和汇率核验时间。')
    }
    if (toolRatesEnabled.value && (!Number.isFinite(toolCnyChecked.getTime())
      || !Number.isFinite(toolUsdChecked.getTime()))) {
      throw new Error('启用 Search/Fetch 费率时，请分别填写有效的人民币和美元价格核验时间。')
    }
    if (form.items.some(item => !Number.isSafeInteger(item.provider_input_cny_micros_per_million)
      || !Number.isSafeInteger(item.provider_output_cny_micros_per_million)
      || !item.provider_input_cny_micros_per_million || !item.provider_output_cny_micros_per_million
      || item.provider_input_cny_micros_per_million < 1 || item.provider_output_cny_micros_per_million < 1)) {
      throw new Error('请填写每个模型已核验的人民币输入和输出价格。')
    }
    const created = await createBillingRateCatalog({
      effective_at: effective.toISOString(), source_url: form.sourceUrl,
      source_checked_at: checked.toISOString(),
      model_cny_source_url: form.modelCnySourceUrl,
      model_cny_source_checked_at: modelCnyChecked.toISOString(),
      fx_source_url: form.fxSourceUrl, fx_source_checked_at: fxChecked.toISOString(),
      fx_cny_per_usd_scaled: Math.round(form.fxCnyPerUsd * 10_000),
      change_note: form.changeNote,
      tool_cny_source_url: toolRatesEnabled.value ? form.toolCnySourceUrl.trim() || null : null,
      tool_cny_source_checked_at: toolRatesEnabled.value ? toolCnyChecked.toISOString() : null,
      tool_usd_source_url: toolRatesEnabled.value ? form.toolUsdSourceUrl.trim() || null : null,
      tool_usd_source_checked_at: toolRatesEnabled.value ? toolUsdChecked.toISOString() : null,
      tool_search_cost_cny_micros_per_success: form.toolSearchCost,
      tool_search_price_milli_per_success: form.toolSearchPrice,
      tool_fetch_cost_cny_micros_per_success: form.toolFetchCost,
      tool_fetch_price_milli_per_success: form.toolFetchPrice,
      items: form.items.map(item => ({ ...item,
        provider_input_cny_micros_per_million: item.provider_input_cny_micros_per_million!,
        provider_output_cny_micros_per_million: item.provider_output_cny_micros_per_million!,
      })),
    }, token)
    if (token !== getToken() || userId !== currentUserId.value) return
    form.changeNote = ''
    catalogs.value = [created, ...catalogs.value.filter(item => item.id !== created.id)]
    await load()
    if (token === getToken() && userId === currentUserId.value) {
      notice.value = `草稿已创建，版本 ${created.version_key.slice(0, 12)}；需要另一位获授权的费率复核人审批。`
    }
  } catch (cause) {
    if (token === getToken() && userId === currentUserId.value) error.value = errorMessage(cause)
  } finally {
    if (token === getToken() && userId === currentUserId.value) busy.value = false
  }
}

async function decide(catalog: BillingRateCatalog, action: 'approve' | 'reject') {
  const token = getToken(), userId = currentUserId.value
  if (!token || !userId) return
  const note = decisionNotes[catalog.id]?.trim() || ''
  if (action === 'reject' && !note) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const changed = action === 'approve'
      ? await approveBillingRateCatalog(catalog.id, token)
      : await rejectBillingRateCatalog(catalog.id, note, token)
    if (token !== getToken() || userId !== currentUserId.value) return
    catalogs.value = catalogs.value.map(item => item.id === changed.id ? changed : item)
    decisionNotes[catalog.id] = ''
    await load()
    if (token === getToken() && userId === currentUserId.value) {
      notice.value = action === 'approve' ? '费率版本已批准，届时会按计划自动切换。'
        : '草稿已拒绝并记录审计事件。'
    }
  } catch (cause) {
    if (token === getToken() && userId === currentUserId.value) error.value = errorMessage(cause)
  } finally {
    if (token === getToken() && userId === currentUserId.value) busy.value = false
  }
}

async function requestCancellation(catalog: BillingRateCatalog) {
  const token = getToken(), userId = currentUserId.value, note = decisionNotes[catalog.id]?.trim()
  if (!token || !userId || !note || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await proposeBillingRateCancellation(catalog.id, note, token)
    if (token !== getToken() || userId !== currentUserId.value) return
    decisionNotes[catalog.id] = ''
    notice.value = '申请已提交，等待另一位费率复核人决定。'
  } catch (cause) {
    if (token === getToken() && userId === currentUserId.value) error.value = errorMessage(cause)
  } finally { if (token === getToken() && userId === currentUserId.value) busy.value = false }
}

watch([() => user.profile?.id, () => user.token], () => {
  generation++
  catalogs.value = []; currentPricing.value = null; error.value = ''; notice.value = ''
  form.changeNote = ''; form.items = []
  Object.keys(decisionNotes).forEach(key => { delete decisionNotes[Number(key)] })
  if (user.profile && user.token) void load()
})
onMounted(() => { alive = true; void load() })
onBeforeUnmount(() => { alive = false; generation++ })
</script>

<style scoped>
.rate-catalog-page { max-width: 1240px; margin: 0 auto; padding: 26px; color: #20243a; }
.page-head, .section-head, .catalog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.page-head { margin-bottom: 20px; }
.page-head h1 { margin: 0 0 8px; font-size: 28px; }
.page-head p:not(.eyebrow), .section-head p, .catalog-head p { margin: 0; color: #707891; line-height: 1.55; }
.eyebrow { margin: 0 0 5px; color: #6675aa; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
button { min-height: 38px; padding: 8px 13px; border: 1px solid #d9deea; border-radius: 9px; background: #fff; color: #30384d; font: inherit; font-weight: 650; cursor: pointer; }
button:hover:not(:disabled) { border-color: #7a8bd1; color: #3347a5; }
button:disabled { cursor: not-allowed; opacity: .55; }
.primary { border-color: #4258bc; background: #4258bc; color: #fff; }
.primary:hover:not(:disabled) { color: #fff; background: #3449a6; }
.danger { border-color: #f0c6ca; color: #a5323d; background: #fff8f8; }
.boundary-note { display: grid; gap: 6px; margin-bottom: 18px; padding: 14px 16px; border: 1px solid #ead9a8; border-radius: 12px; background: #fffaf0; color: #6c5929; line-height: 1.55; }
.panel { margin-bottom: 18px; padding: 18px; border: 1px solid #e5e8f0; border-radius: 14px; background: #fff; box-shadow: 0 5px 18px rgb(29 43 80 / 4%); }
.section-head { align-items: center; margin-bottom: 14px; }
.section-head h2 { margin: 0 0 4px; font-size: 18px; }
.section-head p { font-size: 13px; }
.version-chip, .count { padding: 6px 10px; border-radius: 999px; background: #f0f2fa; color: #54618f; font-size: 12px; white-space: nowrap; }
.current-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.current-grid > div { display: grid; gap: 5px; padding: 12px; border-radius: 10px; background: #f8f9fc; }
.current-grid span, .catalog-facts dt { color: #717991; font-size: 12px; }
.current-grid strong { font-size: 14px; overflow-wrap: anywhere; }
.current-model { grid-column: span 2; grid-template-columns: 1fr auto !important; align-items: center; }
.catalog-form { display: grid; gap: 16px; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; }
.field { display: grid; gap: 6px; color: #4d566e; font-size: 13px; font-weight: 650; }
.field input, .field textarea, .fraction input, .money-input input { width: 100%; min-width: 0; border: 1px solid #dce1ec; border-radius: 8px; padding: 9px 10px; color: #20243a; background: #fff; font: inherit; font-weight: 450; }
.field input:focus, .field textarea:focus, .fraction input:focus, .money-input input:focus { outline: 3px solid rgb(90 111 204 / 18%); border-color: #697bcc; }
.field small, .form-help { color: #7b8296; font-size: 11px; font-weight: 450; line-height: 1.45; }
.financial-basis { margin: -7px 0 0; padding: 9px 11px; border-radius: 8px; background: #f4f6fb; }
.change-note textarea { resize: vertical; }
.tool-rate-editor { display: grid; gap: 13px; padding: 14px; border: 1px solid #e2e6f0; border-radius: 11px; background: #fbfcff; }
.tool-rate-heading { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.tool-rate-heading h3 { margin: 0 0 4px; color: #303a58; font-size: 14px; }
.tool-rate-heading p { margin: 0; color: #747d92; font-size: 12px; line-height: 1.5; }
.tool-rate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.tool-rate-card { display: grid; gap: 10px; min-width: 0; margin: 0; padding: 12px; border: 1px solid #e7eaf2; border-radius: 9px; }
.tool-rate-card legend { padding: 0 5px; color: #3c4c87; font-size: 12px; font-weight: 700; }
.table-wrap { overflow-x: auto; }
.rate-items { border: 1px solid #e7eaf2; border-radius: 10px; }
table { width: 100%; border-collapse: collapse; min-width: 760px; }
th, td { padding: 10px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: middle; }
thead th { color: #707891; background: #f8f9fc; font-size: 12px; font-weight: 700; }
thead small { font-weight: 450; }
tbody th { color: #303a58; font-size: 12px; }
.fraction, .money-input { display: flex; align-items: center; gap: 6px; }
.fraction input { width: 70px; padding-inline: 7px; text-align: center; }
.money-input { border: 1px solid #dce1ec; border-radius: 8px; padding-left: 8px; color: #777f93; }
.money-input input { border: 0; padding-left: 2px; }
.form-actions { display: flex; justify-content: flex-end; }
.catalog-section { display: grid; gap: 2px; }
.catalog-card { margin: 0 0 14px; }
.catalog-title { display: flex; align-items: center; gap: 9px; margin-bottom: 8px; }
.catalog-title code, .catalog-facts code { color: #626d8d; font-size: 11px; overflow-wrap: anywhere; }
.catalog-head h3 { margin: 0 0 5px; font-size: 16px; }
.catalog-head p { max-width: 720px; font-size: 13px; }
.source-age { color: #7b8296; font-size: 11px; white-space: nowrap; }
.status-pill { display: inline-flex; align-items: center; padding: 4px 8px; border-radius: 999px; background: #f2f3f7; color: #5f6678; font-size: 11px; font-weight: 700; white-space: nowrap; }
.status-pill.active { background: #e9f7ef; color: #23784a; }
.status-pill.draft { background: #fff5dc; color: #8b6418; }
.status-pill.approved { background: #eaf0ff; color: #3c56a8; }
.status-pill.rejected, .status-pill.canceled { background: #fbecee; color: #a33b48; }
.catalog-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 16px 0; }
.catalog-facts > div { display: grid; gap: 4px; }
.catalog-facts dd { margin: 0; color: #404a62; font-size: 12px; overflow-wrap: anywhere; }
.catalog-facts a { color: #4057b2; }
.margin-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.tool-margin-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 9px; }
.margin-item { display: grid; gap: 5px; padding: 10px; border-radius: 9px; background: #f7f8fc; }
.margin-item strong { font-size: 11px; overflow-wrap: anywhere; }
.margin-item span { color: #5f6c91; font-size: 12px; }
.alert, .muted, .hint { margin: 13px 0; color: #727b91; font-size: 12px; line-height: 1.5; }
.cost-alert { padding: 10px 12px; border-radius: 9px; background: #fff5df; color: #7a5a1e; }
.decision-box { display: grid; gap: 10px; margin-top: 14px; padding: 13px; border: 1px solid #e8eaf1; border-radius: 10px; background: #fafbfe; }
.decision-note { max-width: 680px; }
.actions { display: flex; flex-wrap: wrap; gap: 9px; }
.events { margin-top: 17px; padding-top: 13px; border-top: 1px solid #edf0f5; }
.events h4 { margin: 0 0 9px; color: #59627a; font-size: 12px; }
.events ol { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.events li { display: grid; grid-template-columns: auto auto 1fr; gap: 8px; color: #727b91; font-size: 11px; line-height: 1.45; }
.events li strong { color: #424d69; }
.events time { white-space: nowrap; }
.events span { overflow-wrap: anywhere; }
.message { margin: 12px 0; padding: 10px 13px; border-radius: 9px; font-size: 13px; }
.message.error { color: #a52f3d; background: #fff0f1; }
.message.notice { color: #286b46; background: #eaf7ef; }
.empty { padding: 18px; color: #7c8498; text-align: center; font-size: 13px; }
@media (max-width: 820px) { .rate-catalog-page { padding: 18px; }.form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.margin-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.catalog-head { display: grid; }.source-age { white-space: normal; } }
@media (max-width: 580px) { .rate-catalog-page { padding: 14px; }.page-head { display: grid; }.page-head h1 { font-size: 23px; }.form-grid, .catalog-facts, .current-grid, .tool-rate-grid, .tool-margin-grid { grid-template-columns: 1fr; }.current-model { grid-column: auto; grid-template-columns: 1fr !important; }.tool-rate-heading { align-items: flex-start; flex-direction: column; }.margin-grid { grid-template-columns: 1fr; }.panel { padding: 14px; }.events li { grid-template-columns: auto 1fr; }.events span { grid-column: 1 / -1; } }
</style>
