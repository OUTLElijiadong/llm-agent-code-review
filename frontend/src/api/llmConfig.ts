import { get, put, post } from './http'

export interface LlmConfig {
  provider: string
  base_url: string
  model: string
  active: boolean
  api_key_masked: string
  is_set: boolean
  source: string // default | global
  fallback_reason: string
  timeout_seconds: number
  max_retries: number
  temperature: number
}

export interface LlmTestResult {
  success: boolean
  message: string
  model: string
  duration_ms: number
  attempts: number
  retryable: boolean
  next_action: string
}

export interface LlmDraft {
  provider?: string
  base_url?: string
  model?: string
  api_key?: string
  timeout_seconds?: number
  max_retries?: number
  temperature?: number
}

export interface LlmModelsResult {
  success: boolean
  message: string
  models: string[]
  selected_model: string
  duration_ms: number
  attempts: number
  fallback: boolean
  retryable: boolean
  next_action: string
}

/** 获取全局 LLM 配置 (管理员) */
export function getLlmConfig(): Promise<LlmConfig> {
  return get<LlmConfig>('/admin/llm/config')
}

/** 更新全局 LLM 配置 (管理员) */
export function updateLlmConfig(data: Partial<{
  provider: string
  base_url: string
  model: string
  api_key: string
  active: boolean
  timeout_seconds: number
  max_retries: number
  temperature: number
}>): Promise<LlmConfig> {
  return put<LlmConfig>('/admin/llm/config', data)
}

/** 测试连接 (留空字段用已保存配置) */
export function testLlmConfig(data: LlmDraft): Promise<LlmTestResult> {
  return post<LlmTestResult>('/admin/llm/test', data)
}

/** 从当前表单或已保存配置拉取 OpenAI-compatible 模型列表。 */
export function fetchLlmModels(data: LlmDraft): Promise<LlmModelsResult> {
  return post<LlmModelsResult>('/admin/llm/models', data)
}
