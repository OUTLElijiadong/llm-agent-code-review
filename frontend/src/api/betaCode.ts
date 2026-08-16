import { del, get, post } from './http'
import type {
  BetaCodePage,
  BetaCodeQuery,
  BetaInviteCode,
  GenerateBetaCodesIn,
  GenerateBetaCodesOut,
} from '@/types/betaCode'

export function generateBetaCodes(body: GenerateBetaCodesIn): Promise<GenerateBetaCodesOut> {
  return post<GenerateBetaCodesOut>('/admin/beta-codes', body)
}

export function listBetaCodes(params: BetaCodeQuery): Promise<BetaCodePage> {
  return get<BetaCodePage>('/admin/beta-codes', params)
}

export function revokeBetaCode(id: number): Promise<BetaInviteCode> {
  return post<BetaInviteCode>(`/admin/beta-codes/${id}/revoke`)
}


export function deleteBetaCode(id: number): Promise<{ id: number; status: string }> {
  return del(`/admin/beta-codes/${id}`)
}
