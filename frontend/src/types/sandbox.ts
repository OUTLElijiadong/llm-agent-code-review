export type SandboxLanguage = 'python' | 'node' | 'java' | 'go' | 'php'
export type SandboxTestMode = 'whitebox' | 'blackbox' | 'combined' | 'deploy'
export type SandboxPurpose = 'test' | 'deploy'

export interface SandboxCreateInput {
  project_id: number
  purpose: SandboxPurpose
  language: SandboxLanguage
  test_mode: SandboxTestMode
  db_type?: 'none' | 'sqlite' | 'mysql'
  worker_code?: string
  source_revision_id?: number
  ttl_hours: number
  remote_target_url?: string
  remote_target_authorized: boolean
}

export interface SandboxEvent {
  id: number
  event_type: string
  stage: string
  message: string
  payload: Record<string, unknown>
  create_time: string
}

export interface SandboxArtifact {
  id: number
  artifact_type: string
  file_name: string
  mime_type: string
  byte_size: number
  sha256: string
}

export interface SandboxEnvironment {
  public_id: string
  project_id: number
  owner_id: number
  worker_code?: string | null
  agent_code: string
  purpose: SandboxPurpose
  language: SandboxLanguage
  test_mode: SandboxTestMode
  status: string
  runtime: string
  source_sha256: string
  preview_path?: string | null
  remote_target_url?: string | null
  expires_at: string
  started_at?: string | null
  stopped_at?: string | null
  result: Record<string, unknown>
  error?: string | null
  events: SandboxEvent[]
  artifacts: SandboxArtifact[]
}

export interface CapabilitySearchResult {
  code: string
  name: string
  description: string
  source: string
  score: number
  aliases: string[]
  agent_code?: string | null
  requires_approval: boolean
}
