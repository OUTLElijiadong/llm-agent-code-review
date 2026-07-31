export type AssetStatus = 'draft' | 'testing' | 'pending_approval' | 'published' | 'disabled' | 'rolled_back' | 'rejected'
export type SkillType = 'llm_transform' | 'readonly_tool' | 'agent_delegate' | 'sequence_workflow'

export interface StudioAsset {
  id: number
  code: string
  name: string
  description?: string
  owner_id: number
  status: AssetStatus
  current_published_version_id?: number
  is_enabled?: number
  create_time: string
  update_time: string
}

export interface StudioVersion {
  id: number
  version_number: number
  checksum: string
  status: AssetStatus
  original_author_id: number
  revised_by?: number
  revision_note?: string
  test_evidence_json?: string
  create_time: string
  update_time: string
}

export interface AgentVersionDetail extends StudioVersion {
  prompt: string
  review_focus: string
  model_config: { temperature: number; max_tokens: number }
  bindings: Array<{ id: number; skill_version_id: number; position: number; config: Record<string, unknown> }>
}

export interface SkillVersionDetail extends StudioVersion {
  skill_type: SkillType
  definition: Record<string, unknown>
  requested_capabilities: string[]
}

export interface CatalogAgent {
  id: number
  code: string
  name: string
  description: string
  owner_id: number
  version_id: number
  version_number: number
  release_id: number
  skills: Array<Record<string, unknown>>
}

export interface AgentReleaseAuthoring {
  prompt: string
  review_focus: string
  model_config: Record<string, unknown>
}

export interface AgentReleaseApproval {
  id: number
  title: string
  status: string
  resource: string
  decision_reason?: string
  agent?: StudioAsset
  version?: StudioVersion
  authoring?: AgentReleaseAuthoring
  previous_authoring?: AgentReleaseAuthoring | null
  before_authoring?: AgentReleaseAuthoring | null
  test_evidence: Record<string, unknown>
  test_evidence_kind?: string
  dependencies: Array<Record<string, unknown>>
  diff: {
    kind?: 'initial' | 'update' | string
    prompt_changed: boolean
    review_focus_changed: boolean
    model_config_changed: boolean
    from_version?: number
    to_version?: number
    before?: AgentReleaseAuthoring | null
    after?: AgentReleaseAuthoring | null
  }
  estimated_calls_per_chunk: number
  risk: { level: string; requested_capabilities: string[] }
}

export interface ReleaseRecord {
  id: number
  agent_id: number
  agent_version_id: number
  previous_release_id?: number
  rollback_of_release_id?: number
  package_checksum: string
  status: string
  published_by: number
  published_at: string
}

export interface AdminAgentReleases {
  agent: StudioAsset
  releases: ReleaseRecord[]
}
