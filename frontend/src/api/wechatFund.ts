import { get, post } from './http'

export type FundAccount = 'BASIC' | 'OPERATION' | 'FEES'
export interface FundBatch {
  id: number; bill_date: string; account_type: FundAccount; source_origin: 'wechat_api' | 'manual_upload'
  file_sha256: string; file_size: number; provider_hash_type: string | null; row_count: number
  income_count: number; income_fen: string; expense_count: number; expense_fen: string
  fee_debit_fen: string; fee_refund_fen: string; imported_by: number; imported_at: string
  verification: 'file_totals_only'; file_versions?: number
}
export interface FundRow {
  id: number; source_line_number: number; booked_at: string; provider_business_no: string
  funding_flow_no: string; business_name: string; business_type: string; direction: string
  amount_fen: string; balance_fen: string; business_voucher_no: string
}
export function listFundBatches(token: string | null, offset = 0) {
  return get<{ items: FundBatch[]; total: number }>('/billing/admin/fund-statements',
    { limit: 50, offset }, undefined, false, { authTokenSnapshot: token })
}
export function listFundRows(id: number, token: string | null, offset = 0) {
  return get<{ items: FundRow[] }>(`/billing/admin/fund-statements/${id}/rows`,
    { limit: 100, offset }, undefined, false, { authTokenSnapshot: token })
}
export function importFundBill(input: { date: string; account: FundAccount; file: File }, token: string | null) {
  const form = new FormData()
  form.append('bill_date', input.date); form.append('account_type', input.account); form.append('file', input.file)
  return post<FundBatch>('/billing/admin/fund-statements/import', form, undefined, undefined, false,
    { authTokenSnapshot: token })
}
export function downloadFundBill(input: { date: string; account: FundAccount }, token: string | null) {
  return post<FundBatch>('/billing/admin/fund-statements/download',
    { bill_date: input.date, account_type: input.account }, undefined, undefined, false, { authTokenSnapshot: token })
}
