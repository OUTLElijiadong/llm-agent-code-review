import { get, put, post } from './http'

export interface LlmConfig {
  provider: string
  base_url: string
  model: string
  active: boolean
  api_key_masked: string
  is_set: boolean
  source: string // default | global
}

export interface LlmTestResult {
  success: boolean
  message: string
  model: string
  duration_ms: number
}

/** 获取全局 LLM 配置 (管理员) */
export function getLlmConfig(): Promise<LlmConfig> {
  return get<LlmConfig>('/admin/llm/config')
}

/** 更新全局 LLM 配置 (管理员) */
export function updateLlmConfig(data: Partial<{
  provider: string; base_url: string; model: string; api_key: string; active: boolean
}>): Promise<LlmConfig> {
  return put<LlmConfig>('/admin/llm/config', data)
}

/** 测试连接 (留空字段用已保存配置) */
export function testLlmConfig(data: {
  base_url?: string; model?: string; api_key?: string
}): Promise<LlmTestResult> {
  return post<LlmTestResult>('/admin/llm/test', data)
}
