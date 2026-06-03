import { get } from './http'
import type { Page } from '@/types/common'
import type { AiLogOut, AiLogDetailOut } from '@/types/aiLog'

export function getAiLogs(params?: Record<string, unknown>) {
  return get<Page<AiLogOut>>('/ai-logs', params)
}

export function getAiLogDetail(logId: number) {
  return get<AiLogDetailOut>(`/ai-logs/${logId}`)
}
