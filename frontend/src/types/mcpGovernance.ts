import type { SandboxLanguage, SandboxTestMode } from './sandbox'

export type McpTransport = 'streamable_http' | 'managed'
export type McpRisk = 'low' | 'medium' | 'high' | 'critical'

export interface McpServer {
  id: number
  code: string
  name: string
  description: string
  transport: McpTransport
  url: string
  auth_type: 'none' | 'bearer' | 'headers' | 'oauth_required'
  has_credentials: boolean
  managed_kind?: 'prism-code' | 'prism-sandbox' | 'playwright' | null
  status: string
  enabled: boolean
  credential_required: boolean
  last_health_at?: string | null
  last_error?: string | null
  tool_count: number
}

export interface McpServerInput {
  code: string
  name: string
  description: string
  transport: McpTransport
  url: string
  auth_type: McpServer['auth_type']
  headers?: Record<string, string>
  managed_kind?: McpServer['managed_kind']
  enabled: boolean
  credential_required: boolean
}

export interface McpTool {
  id: number
  server_id: number
  server_code: string
  server_status: string
  tool_name: string
  model_name: string
  display_name: string
  description: string
  input_schema: Record<string, unknown>
  schema_sha256: string
  risk_level: McpRisk
  enabled: boolean
}

export interface McpBinding {
  id: number
  agent_code: string
  tool_id: number
  tool_code: string
  server_code: string
  permission: 'allow' | 'deny' | 'escalate'
  requires_approval: boolean
  schema_current: boolean
  enabled: boolean
}

export interface CapabilityAlias {
  id: number
  capability_code: string
  alias: string
  locale: string
  weight: number
  enabled: boolean
}

export interface SandboxWorker {
  id: number
  code: string
  name: string
  worker_type: 'local' | 'managed' | 'production_fallback'
  transport: 'unix' | 'https'
  endpoint: string
  supported_languages: SandboxLanguage[]
  supported_modes: SandboxTestMode[]
  runtime: string
  max_concurrency: number
  priority: number
  status: string
  enabled: boolean
  last_seen_at?: string | null
  last_error?: string | null
  fingerprint: Record<string, unknown>
}

export interface SandboxWorkerInput {
  code: string
  name: string
  worker_type: SandboxWorker['worker_type']
  transport: SandboxWorker['transport']
  endpoint: string
  token?: string
  supported_languages: SandboxLanguage[]
  supported_modes: SandboxTestMode[]
  runtime: string
  max_concurrency: number
  priority: number
  enabled: boolean
}
