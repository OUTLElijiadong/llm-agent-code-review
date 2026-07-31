import type { Page } from './common'

export type BetaCodeStatus = 'active' | 'used' | 'revoked' | 'expired'

export interface BetaInviteCode {
  id: number
  display_prefix: string
  label?: string
  status: BetaCodeStatus
  expires_at: string
  created_by: number
  used_by?: number
  used_at?: string
  create_time: string
}

export interface GenerateBetaCodesIn {
  count: number
  expiry_days: number
  label?: string
}

export interface GenerateBetaCodesOut {
  codes: string[]
  items: BetaInviteCode[]
}

export interface BetaCodeQuery {
  status?: BetaCodeStatus | ''
  page?: number
  page_size?: number
}

export type BetaCodePage = Page<BetaInviteCode>
