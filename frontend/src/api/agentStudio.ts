import { del, get, post } from './http'
import type {
  AdminAgentReleases,
  AgentReleaseApproval,
  AgentVersionDetail,
  CatalogAgent,
  ReleaseRecord,
  SkillType,
  SkillVersionDetail,
  StudioAsset,
  StudioVersion,
} from '@/types/agentStudio'

export function listStudioAgents(): Promise<StudioAsset[]> {
  return get<StudioAsset[]>('/agent-studio/agents')
}

export function listStudioSkills(): Promise<StudioAsset[]> {
  return get<StudioAsset[]>('/agent-studio/skills')
}

export function createStudioAgent(body: {
  code: string; name: string; description: string; prompt: string; review_focus: string
  model_config_json: { temperature: number; max_tokens: number }
}): Promise<{ agent: StudioAsset; version: StudioVersion }> {
  return post('/agent-studio/agents', body)
}

export function createStudioSkill(body: {
  code: string; name: string; description: string; skill_type: SkillType
  definition: Record<string, unknown>; requested_capabilities: string[]
}): Promise<{ skill: StudioAsset; version: StudioVersion }> {
  return post('/agent-studio/skills', body)
}

export function listAgentVersions(agentId: number): Promise<StudioVersion[]> {
  return get(`/agent-studio/agents/${agentId}/versions`)
}

export function listSkillVersions(skillId: number): Promise<StudioVersion[]> {
  return get(`/agent-studio/skills/${skillId}/versions`)
}

export function getAgentVersion(versionId: number): Promise<AgentVersionDetail> {
  return get(`/agent-studio/agent-versions/${versionId}`)
}

export function getSkillVersion(versionId: number): Promise<SkillVersionDetail> {
  return get(`/agent-studio/skill-versions/${versionId}`)
}

export function bindStudioSkill(versionId: number, body: {
  skill_version_id: number; position: number; config: Record<string, unknown>
}): Promise<Record<string, unknown>> {
  return post(`/agent-studio/agent-versions/${versionId}/skills`, body)
}

export function unbindStudioSkill(bindingId: number): Promise<Record<string, unknown>> {
  return del(`/agent-studio/bindings/${bindingId}`)
}

export function testStudioAgent(versionId: number): Promise<StudioVersion> {
  return post(`/agent-studio/agent-versions/${versionId}/test`, { sample_output: { issues: [] } })
}

export function submitStudioAgent(versionId: number, note: string): Promise<{ approval_id: number; status: string }> {
  return post(`/agent-studio/agent-versions/${versionId}/submit`, { note })
}

export function withdrawStudioAgent(versionId: number, note: string): Promise<StudioVersion> {
  return post(`/agent-studio/agent-versions/${versionId}/withdraw`, { note })
}

export function listAgentCatalog(): Promise<CatalogAgent[]> {
  return get('/agent-catalog')
}

export function listAgentReleaseApprovals(): Promise<AgentReleaseApproval[]> {
  return get('/admin/agent-releases')
}

export function listAdminAgentReleases(): Promise<AdminAgentReleases[]> {
  return get('/admin/agent-releases/agents')
}

export function approveAgentRelease(id: number, note: string): Promise<ReleaseRecord> {
  return post(`/admin/agent-releases/${id}/approve`, { note })
}

export function rejectAgentRelease(id: number, note: string): Promise<Record<string, unknown>> {
  return post(`/admin/agent-releases/${id}/reject`, { note })
}

export function reviseAgentRelease(id: number, body: {
  prompt: string; review_focus: string; model_config_json: Record<string, unknown>; note: string
}): Promise<StudioVersion> {
  return post(`/admin/agent-releases/${id}/revise`, body)
}

export function disableCustomAgent(agentId: number): Promise<StudioAsset> {
  return post(`/admin/agent-releases/agents/${agentId}/disable`)
}

export function rollbackCustomAgent(agentId: number, releaseId: number): Promise<ReleaseRecord> {
  return post(`/admin/agent-releases/agents/${agentId}/rollback/${releaseId}`)
}
