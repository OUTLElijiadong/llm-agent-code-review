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

/* ── 模型注册表与角色分配 ── */

export interface ModelRegistryItem {
  id: string
  label: string
  vision: boolean
  source: 'pulled' | 'manual'
  capability_source?: 'official' | 'manual' | 'unverified'
  added_at: string
}

export interface ModelRegistryView {
  models: ModelRegistryItem[]
  assignments: Record<string, string>
  roles: Record<string, string>
  warnings?: string[]
}

export interface RegistrySyncResult {
  success: boolean
  message: string
  fetched: string[]
  added: string[]
  models: ModelRegistryItem[]
}

/** 模型注册表 + 角色分配现状 */
export function getModelRegistry(): Promise<ModelRegistryView> {
  return get<ModelRegistryView>('/admin/llm/models/registry')
}

/** 从 provider 拉取最新模型并合并进注册表 */
export function syncModelRegistry(data: LlmDraft): Promise<RegistrySyncResult> {
  return post<RegistrySyncResult>('/admin/llm/models/registry/sync', data)
}

/** 整表保存注册表(手工增删/能力标记) */
export function saveModelRegistry(models: Array<{ id: string; label?: string; vision?: boolean }>): Promise<ModelRegistryView> {
  return put<ModelRegistryView>('/admin/llm/models/registry', { models })
}

/** 更新角色->模型分配(空串清除) */
export function saveModelAssignments(assignments: Record<string, string>): Promise<ModelRegistryView> {
  return put<ModelRegistryView>('/admin/llm/models/assignments', { assignments })
}
