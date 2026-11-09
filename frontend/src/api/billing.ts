import { get, post, put } from './http'

export interface BillingPricing {
  currency: 'CNY'
  pricing_version: string
  pricing_effective_at: string
  next_pricing_effective_at: string | null
  billing_unit: {
    unit: 'milli_credit'
    milli_per_credit: number
    rate_unit: 'milli_credit_per_token'
    rounding: 'ceil_per_model_request'
  }
  credit_pack: { credits: number; price_fen: number }
  reviewer_month: { price_fen: number; credits: number }
  enterprise_seat_month: { price_fen: number; credits: number }
  model_milli_per_token: Record<string, { input: number; output: number }>
  model_pricing_ready?: boolean
  tool_milli_credits_per_success: { search: number | null; fetch: number | null }
  billable_models: string[]
  channels: { alipay: boolean; wechat: boolean }
  sales_enabled: boolean
}

export interface BillingOrder {
  order_no: string
  product: 'personal_credits' | 'reviewer_month' | 'enterprise_credits' | 'enterprise_create' | 'enterprise_renew'
  amount_fen: number
  quantity: number
  channel: 'alipay' | 'wechat'
  enterprise_id?: number | null
  status: string
  payment_status: string
  payment_code_url?: string | null
  payment_expires_at?: string | null
  created_at: string
}

export interface BillingRefund {
  id?: number
  order_no: string
  refund_no: string
  amount_fen: number
  reason: string
  status: string
  review_note?: string | null
  provider_status?: string | null
  attempt_count: number
  requires_reconciliation?: boolean
  created_at: string
  updated_at: string
  completed_at?: string | null
  product?: BillingOrder['product']
}

export interface BillingStatementBatch {
  id: number
  channel: 'alipay' | 'wechat'
  source_origin: 'manual_upload' | 'alipay_api' | 'wechat_api'
  bill_date: string
  bill_type: string
  file_sha256: string
  provider_hash_type?: 'SHA1' | null
  provider_hash_value?: string | null
  file_name: string
  file_size: number
  parser_version: string
  row_count: number
  payment_total_fen: number
  refund_total_fen: number
  received_total_fen: number
  received_missing_count: number
  fee_total_micros: number
  fee_missing_count: number
  imported_by: number
  imported_at: string
}

export interface BillingStatementRow {
  id: number
  source_line_number: number
  record_type: 'payment' | 'refund'
  order_no: string
  provider_trade_id?: string | null
  refund_no?: string | null
  provider_refund_id?: string | null
  provider_status: string
  currency: string
  amount_fen: number
  received_fen?: number | null
  fee_micros?: number | null
  local_order_status?: string | null
  local_amount_fen?: number | null
  local_trade_id?: string | null
  local_refund_no?: string | null
  local_provider_refund_id?: string | null
  local_refund_status?: string | null
  local_refund_amount_fen?: number | null
  discrepancy_code: string
  review_status: 'unreviewed' | 'acknowledge' | 'investigating' | 'resolved_external'
  last_reviewed_by?: number | null
  last_reviewed_at?: string | null
  last_review_note?: string | null
}

export interface BillingStatementReviewEvent {
  id: number
  actor_id: number
  decision: 'acknowledge' | 'investigating' | 'resolved_external'
  note: string
  created_at: string
}

export type BillingStatementReviewDecision = BillingStatementReviewEvent['decision']

export interface BillingReservationReconciliationEvent {
  id: number
  reservation_id: number
  related_event_id: number | null
  actor_id: number
  decision: 'proposed' | 'approved' | 'rejected' | 'blocked' | 'recovered'
  resolution: 'settle' | 'release_not_sent' | null
  provider_request_id: string | null
  input_tokens: number | null
  output_tokens: number | null
  tool_successful_calls: 0 | 1 | null
  actual_milli: number | null
  evidence_reference: string | null
  evidence_sha256: string | null
  note: string
  created_at: string
}

export interface BillingReservationReconciliation {
  id: number
  request_key_suffix: string
  model_name: string
  billable_kind: 'model' | 'search' | 'fetch'
  tool_name: 'search' | 'fetch' | null
  status: 'reserved' | 'unknown' | 'settled' | 'released'
  usage_state: string
  user_id: number | null
  enterprise_id: number | null
  group_id: number | null
  usage_kind: string
  created_at: string
  requested_milli: number
  held_milli: number
  actual_milli: number | null
  input_tokens: number | null
  output_tokens: number | null
  server_tool_search_calls: number | null
  server_tool_fetch_calls: number | null
  tool_cost_cny_micros_per_success: number | null
  tool_price_milli_per_success: number | null
  tool_successful_calls: 0 | 1 | null
  provider_attempt_started_at: string | null
  pricing_version: string | null
  provider_identity_version: string | null
  has_rate_snapshot: boolean
  has_tool_rate_snapshot: boolean
  has_provider_snapshot: boolean
  can_settle: boolean
  can_release_not_sent: boolean
  can_recover_stale_attempt: boolean
  settlement_block_reasons: string[]
  release_block_reasons: string[]
  pending_proposal: BillingReservationReconciliationEvent | null
}

export interface BillingReservationReconciliationPage {
  items: BillingReservationReconciliation[]
  total: number
  limit: number
  offset: number
}

export interface BillingC4Review {
  reservation: {
    id: number
    status: string
    billable_kind: 'model' | 'search' | 'fetch'
    model_name: string
    requested_milli: number
    pricing_version: string | null
  }
  proposal: BillingReservationReconciliationEvent
}

export interface BillingC4Reviewer {
  user_id: number
  active: boolean
  eligible: boolean
  username: string | null
  nickname: string | null
  granted_at: string
  revoked_at: string | null
}

export interface BillingStatementPage<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export type RefundStatusFilter = 'requested' | 'manual_review' | 'approved' | 'processing' | 'unknown'
  | 'failed' | 'succeeded' | 'rejected'

export interface BillingRateCatalogItem {
  model_name: string
  input_rate_numerator: number
  input_rate_denominator: number
  output_rate_numerator: number
  output_rate_denominator: number
  provider_input_usd_micros_per_million: number
  provider_output_usd_micros_per_million: number
  provider_input_cny_micros_per_million: number | null
  provider_output_cny_micros_per_million: number | null
}

export interface BillingRateCatalogEvent {
  actor_id: number | null
  action: 'created' | 'approved' | 'rejected' | 'canceled'
  note: string
  created_at: string
}

export interface BillingRateCatalog {
  id: number
  version_key: string
  status: 'draft' | 'approved' | 'rejected' | 'canceled'
  effective_at: string
  source_url: string
  source_checked_at: string
  model_cny_source_url: string | null
  model_cny_source_checked_at: string | null
  fx_source_url: string
  fx_source_checked_at: string
  source_sha256: string
  fx_cny_per_usd: number
  credit_pack_price_fen: number
  credit_pack_credits: number
  tool_cny_source_url: string | null
  tool_cny_source_checked_at: string | null
  tool_usd_source_url: string | null
  tool_usd_source_checked_at: string | null
  tool_search_cost_cny_micros_per_success: number
  tool_search_price_milli_per_success: number
  tool_fetch_cost_cny_micros_per_success: number
  tool_fetch_price_milli_per_success: number
  change_note: string
  created_by: number | null
  created_at: string
  approved_by: number | null
  approved_at: string | null
  rejected_by: number | null
  rejected_at: string | null
  canceled_by: number | null
  canceled_at: string | null
  transition_note: string | null
  minimum_direct_margin_bps: number
  margin_bps: Record<string, { input_bps: number; output_bps: number }>
  tool_margin_bps: { search: number | null; fetch: number | null }
  supplier_price_diff: {
    changes: Array<{ model_name: string; channel: 'input' | 'output' | 'input_cny' | 'output_cny'; previous: number; current: number }>
    increases: Array<{ model_name: string; channel: 'input' | 'output' | 'input_cny' | 'output_cny'; previous: number; current: number }>
    decreases: Array<{ model_name: string; channel: 'input' | 'output' | 'input_cny' | 'output_cny'; previous: number; current: number }>
    tool_changes: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
    tool_increases: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
    tool_decreases: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
  }
  tool_supplier_price_diff: {
    changes: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
    increases: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
    decreases: Array<{ tool: 'search' | 'fetch'; previous: number; current: number }>
  }
  items: BillingRateCatalogItem[]
  events: BillingRateCatalogEvent[]
}

export function listBillingRateReviewers(limit: number, offset: number, token: string | null) {
  return get<BillingStatementPage<BillingC4Reviewer>>('/billing/admin/rate-reviewers', { limit, offset }, undefined, false, { authTokenSnapshot: token })
}

export function changeBillingRateReviewer(id: number, active: boolean, reason: string, token: string | null) {
  return put<{ user_id: number; active: boolean }>(`/billing/admin/rate-reviewers/${id}`, { active, reason }, { authTokenSnapshot: token })
}

export function listBillingRefundReviewers(limit: number, offset: number, token: string | null) {
  return get<BillingStatementPage<BillingC4Reviewer>>('/billing/admin/refund-reviewers', { limit, offset }, undefined, false, { authTokenSnapshot: token })
}

export function changeBillingRefundReviewer(id: number, active: boolean, reason: string, token: string | null) {
  return put<{ user_id: number; active: boolean }>(`/billing/admin/refund-reviewers/${id}`, { active, reason }, { authTokenSnapshot: token })
}

export function listBillingRefundReviews(token: string | null, beforeId?: number) {
  return get<{ items: BillingRefund[]; next_before_id: number | null }>('/billing/refund-reviews',
    { limit: 50, before_id: beforeId }, undefined, false, { authTokenSnapshot: token })
}

export function listBillingRateReviews(token: string | null) {
  return get<{ items: BillingRateCatalog[] }>('/billing/rate-reviews', undefined, undefined, false, { authTokenSnapshot: token })
}

export function getBillingRateReview(id: number, token: string | null) {
  return get<BillingRateCatalog>(`/billing/rate-reviews/${id}`, undefined, undefined, false, { authTokenSnapshot: token })
}

export function decideBillingRateReview(id: number, decision: 'approve' | 'reject', note: string, token: string | null) {
  if (decision === 'approve') {
    return post<BillingRateCatalog>(`/billing/rate-reviews/${id}/approve`, undefined,
      undefined, undefined, false, { authTokenSnapshot: token })
  }
  return post<BillingRateCatalog>(`/billing/rate-reviews/${id}/reject`, { note },
    undefined, undefined, false, { authTokenSnapshot: token })
}

export interface BillingRateCancellation {
  id: number
  catalog_id: number
  status: 'pending' | 'approved' | 'rejected' | 'expired'
  proposed_by: number
  proposal_note: string
  proposed_at: string
  decided_by: number | null
  decision_note: string | null
  decided_at: string | null
}

export function proposeBillingRateCancellation(id: number, note: string, token: string | null) {
  return post<BillingRateCancellation>(`/billing/rate-catalogs/${id}/cancel-requests`, { note }, undefined, undefined, false, { authTokenSnapshot: token })
}

export type BillingRateCancellationCatalog = Pick<BillingRateCatalog, 'id' | 'version_key' | 'status' | 'effective_at' | 'source_url' | 'source_checked_at' | 'model_cny_source_url' | 'model_cny_source_checked_at' | 'fx_source_url' | 'fx_source_checked_at' | 'source_sha256' | 'fx_cny_per_usd' | 'credit_pack_price_fen' | 'credit_pack_credits' | 'items' | 'tool_cny_source_url' | 'tool_cny_source_checked_at' | 'tool_usd_source_url' | 'tool_usd_source_checked_at' | 'tool_search_cost_cny_micros_per_success' | 'tool_search_price_milli_per_success' | 'tool_fetch_cost_cny_micros_per_success' | 'tool_fetch_price_milli_per_success' | 'change_note'>
export type BillingRateCancellationReview = BillingRateCancellation & { catalog: BillingRateCancellationCatalog }

export function listBillingRateCancellations(token: string | null) {
  return get<{ items: BillingRateCancellationReview[] }>('/billing/rate-cancel-requests', undefined, undefined, false, { authTokenSnapshot: token })
}

export function decideBillingRateCancellation(id: number, decision: 'approve' | 'reject', note: string, token: string | null) {
  return post<BillingRateCancellation>(`/billing/rate-cancel-requests/${id}/decision`, { decision, note }, undefined, undefined, false, { authTokenSnapshot: token })
}

export function getBillingPricing() {
  return get<BillingPricing>('/billing/pricing')
}

export function listBillingRateCatalogs(token?: string | null) {
  return get<{ items: BillingRateCatalog[] }>(
    '/billing/admin/rate-catalogs', undefined, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token },
  )
}

export function createBillingRateCatalog(input: {
  effective_at: string
  source_url: string
  source_checked_at: string
  model_cny_source_url: string
  model_cny_source_checked_at: string
  fx_source_url: string
  fx_source_checked_at: string
  fx_cny_per_usd_scaled: number
  change_note: string
  tool_cny_source_url: string | null
  tool_cny_source_checked_at: string | null
  tool_usd_source_url: string | null
  tool_usd_source_checked_at: string | null
  tool_search_cost_cny_micros_per_success: number
  tool_search_price_milli_per_success: number
  tool_fetch_cost_cny_micros_per_success: number
  tool_fetch_price_milli_per_success: number
  items: Array<Omit<BillingRateCatalogItem, 'provider_input_cny_micros_per_million' | 'provider_output_cny_micros_per_million'> & {
    provider_input_cny_micros_per_million: number
    provider_output_cny_micros_per_million: number
  }>
}, token?: string | null) {
  return post<BillingRateCatalog>('/billing/admin/rate-catalogs', input,
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function approveBillingRateCatalog(catalogId: number, token?: string | null) {
  return post<BillingRateCatalog>(`/billing/admin/rate-catalogs/${catalogId}/approve`, {},
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function rejectBillingRateCatalog(catalogId: number, note: string, token?: string | null) {
  return post<BillingRateCatalog>(`/billing/admin/rate-catalogs/${catalogId}/reject`, { note },
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function getBillingWallet() {
  return get<{ available_milli: number }>('/billing/wallet')
}

export function getEnterpriseBillingWallet(enterpriseId: number) {
  return get<{ available_milli: number }>(`/billing/enterprises/${enterpriseId}/wallet`, undefined, [40300, 40301, 40303, 40400])
}

export function getBillingEntitlement() {
  return get<{ reviewer: boolean; enforcement_enabled: boolean }>('/billing/entitlement')
}

export function getBillingOrders() {
  return get<BillingOrder[]>('/billing/orders')
}

export function getEnterpriseBillingOrders(enterpriseId: number) {
  return get<BillingOrder[]>(`/billing/enterprises/${enterpriseId}/orders`, undefined, [40300, 40301, 40303, 40400])
}

export function getBillingOrder(orderNo: string) {
  return get<BillingOrder>(`/billing/orders/${encodeURIComponent(orderNo)}`)
}

export function createBillingOrder(input: {
  product: BillingOrder['product']
  quantity: number
  channel: BillingOrder['channel']
  client_request_id: string
  enterprise_id?: number
  enterprise_name?: string
}) {
  return post<BillingOrder>('/billing/orders', input)
}

export function refreshBillingOrder(orderNo: string) {
  return post<BillingOrder>(`/billing/orders/${encodeURIComponent(orderNo)}/refresh`)
}

export function cancelBillingOrder(orderNo: string) {
  return post<BillingOrder>(`/billing/orders/${encodeURIComponent(orderNo)}/cancel`)
}

export function requestBillingRefund(orderNo: string, reason: string) {
  return post<BillingRefund>(`/billing/orders/${encodeURIComponent(orderNo)}/refunds`, { reason })
}

export function getBillingRefund(orderNo: string) {
  return get<BillingRefund | null>(`/billing/orders/${encodeURIComponent(orderNo)}/refunds`)
}

export function listBillingRefunds(status: RefundStatusFilter, token: string | null, beforeId?: number) {
  return get<BillingRefund[]>('/billing/admin/refunds', { status, before_id: beforeId }, undefined, false, { authTokenSnapshot: token })
}

export function decideBillingRefund(refundNo: string, decision: 'approve' | 'reject', reason: string, token: string | null) {
  return post<BillingRefund>(`/billing/admin/refunds/${encodeURIComponent(refundNo)}/decision`, { decision, reason },
    undefined, undefined, false, { authTokenSnapshot: token })
}

export function executeBillingRefund(refundNo: string, token: string | null) {
  return post<BillingRefund>(`/billing/admin/refunds/${encodeURIComponent(refundNo)}/execute`, undefined,
    undefined, undefined, false, { authTokenSnapshot: token })
}

export function reconcileBillingRefund(refundNo: string, token: string | null) {
  return post<BillingRefund>(`/billing/admin/refunds/${encodeURIComponent(refundNo)}/reconcile`, undefined,
    undefined, undefined, false, { authTokenSnapshot: token })
}

export function listBillingStatements(channel?: BillingStatementBatch['channel'], token?: string | null,
                                      params: { limit?: number; offset?: number } = {}) {
  return get<BillingStatementPage<BillingStatementBatch>>(
    '/billing/admin/statements', { channel, limit: 50, offset: 0, ...params }, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export function importBillingStatement(input: {
  channel: BillingStatementBatch['channel']
  billDate: string
  billType: string
  file: File
}, token?: string | null) {
  const form = new FormData()
  form.append('channel', input.channel)
  form.append('bill_date', input.billDate)
  form.append('bill_type', input.billType)
  form.append('file', input.file)
  return post<BillingStatementBatch>('/billing/admin/statements/import', form,
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function fetchBillingStatement(input: {
  channel: BillingStatementBatch['channel']
  billDate: string
  billType: string
}, token?: string | null) {
  return post<BillingStatementBatch>('/billing/admin/statements/download', {
    channel: input.channel, bill_date: input.billDate, bill_type: input.billType,
  }, undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function listBillingStatementRows(batchId: number, params: {
  discrepancy_code?: string
  review_status?: string
  limit?: number
  offset?: number
} = {}, token?: string | null) {
  return get<BillingStatementPage<BillingStatementRow>>(
    `/billing/admin/statements/${batchId}/rows`, { limit: 100, offset: 0, ...params }, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export function reviewBillingStatementRow(batchId: number, rowId: number,
                                          decision: BillingStatementReviewDecision, note: string,
                                          token?: string | null) {
  return post<BillingStatementRow>(`/billing/admin/statements/${batchId}/rows/${rowId}/review`,
    { decision, note }, undefined, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export function listBillingStatementReviews(rowId: number, token?: string | null) {
  return get<{ items: BillingStatementReviewEvent[] }>(`/billing/admin/statements/rows/${rowId}/reviews`,
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token })
}

export function listBillingReservationReconciliation(status?: 'reserved' | 'unknown' | 'reviewed', token?: string | null,
                                                     params: { limit?: number; offset?: number;
                                                       billable_kind?: 'model' | 'search' | 'fetch' } = {}) {
  return get<BillingReservationReconciliationPage>(
    '/billing/admin/reservations/reconciliation', { status, limit: 100, offset: 0, ...params },
    undefined, false, token === undefined ? undefined : { authTokenSnapshot: token },
  )
}

export function listBillingReservationReconciliationEvents(reservationId: number, token?: string | null) {
  return get<{ items: BillingReservationReconciliationEvent[] }>(
    `/billing/admin/reservations/${reservationId}/reconciliation/events`,
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token },
  )
}

export function listBillingC4Reviews(limit: number, offset: number, token: string | null) {
  return get<{ items: BillingC4Review[] }>('/billing/reconciliation/reviews', { limit, offset }, undefined, false,
    { authTokenSnapshot: token })
}

export function getBillingC4Review(reservationId: number, token: string | null) {
  return get<BillingC4Review>(`/billing/reconciliation/reviews/${reservationId}`, undefined, undefined, false,
    { authTokenSnapshot: token })
}

export function listBillingC4Reviewers(limit: number, offset: number, token: string | null) {
  return get<{ items: BillingC4Reviewer[]; total: number; limit: number; offset: number }>(
    '/billing/admin/reconciliation/reviewers', { limit, offset }, undefined, false, { authTokenSnapshot: token },
  )
}

export function changeBillingC4Reviewer(userId: number, active: boolean, reason: string, token: string | null) {
  return put<{ user_id: number; active: boolean }>(`/billing/admin/reconciliation/reviewers/${userId}`,
    { active, reason }, { authTokenSnapshot: token })
}

export function proposeBillingReservationReconciliation(reservationId: number, input: {
  resolution: 'settle' | 'release_not_sent'
  provider_request_id?: string
  input_tokens?: number
  output_tokens?: number
  tool_successful_calls?: 0 | 1
  evidence_reference: string
  evidence_sha256: string
  note: string
}, token?: string | null) {
  return post<BillingReservationReconciliationEvent>(
    `/billing/admin/reservations/${reservationId}/reconciliation/proposals`, input,
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token },
  )
}

export function decideBillingReservationReconciliation(reservationId: number,
                                                       decision: 'approve' | 'reject', note: string,
                                                       token?: string | null) {
  return post<{ reservation: BillingReservationReconciliation; event: BillingReservationReconciliationEvent }>(
    `/billing/admin/reservations/${reservationId}/reconciliation/decision`, { decision, note },
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token },
  )
}

export function recoverStaleBillingReservationAttempt(reservationId: number, note: string, token?: string | null) {
  return post<BillingReservationReconciliation>(
    `/billing/admin/reservations/${reservationId}/reconciliation/recover-stale`, { note },
    undefined, undefined, false, token === undefined ? undefined : { authTokenSnapshot: token },
  )
}
