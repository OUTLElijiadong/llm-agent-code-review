import { get, post, put, del } from './http'

export interface ApiConfigOut {
  provider: string
  api_key_masked: string
  base_url: string
  model: string
  is_active: boolean
  is_custom: boolean
  created_at?: string
  updated_at?: string
}

export interface ApiConfigSaveIn {
  provider: string
  api_key: string
  base_url: string
  model: string
}

export interface ApiConfigTestIn {
  provider: string
  api_key: string
  base_url: string
  model: string
}

export interface ApiConfigTestOut {
  success: boolean
  message: string
  model: string
  duration_ms: number
}

export function getApiConfig(): Promise<ApiConfigOut> {
  return get<ApiConfigOut>('/api-config')
}

export function saveApiConfig(payload: ApiConfigSaveIn): Promise<ApiConfigOut> {
  return put<ApiConfigOut>('/api-config', payload)
}

export function deleteApiConfig(): Promise<string> {
  return del<string>('/api-config')
}

export function testApiConnection(payload: ApiConfigTestIn): Promise<ApiConfigTestOut> {
  return post<ApiConfigTestOut>('/api-config/test', payload)
}
