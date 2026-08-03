import { del, get, post, put } from './http'
import type {
  CapabilityAlias,
  McpBinding,
  McpServer,
  McpServerInput,
  McpTool,
  McpRisk,
  SandboxWorker,
  SandboxWorkerInput,
} from '@/types/mcpGovernance'

const BASE = '/admin/mcp'

export function listMcpServers(): Promise<McpServer[]> {
  return get<McpServer[]>(`${BASE}/servers`)
}

export function seedRecommendedMcpServers(): Promise<McpServer[]> {
  return post<McpServer[]>(`${BASE}/servers/recommended`)
}

export function createMcpServer(data: McpServerInput): Promise<McpServer> {
  return post<McpServer>(`${BASE}/servers`, data)
}

export function updateMcpServer(id: number, data: McpServerInput): Promise<McpServer> {
  return put<McpServer>(`${BASE}/servers/${id}`, data)
}

export function deleteMcpServer(id: number): Promise<void> {
  return del<void>(`${BASE}/servers/${id}`)
}

export function checkMcpServer(id: number): Promise<McpServer> {
  return post<McpServer>(`${BASE}/servers/${id}/health`)
}

export function syncMcpTools(id: number): Promise<McpTool[]> {
  return post<McpTool[]>(`${BASE}/servers/${id}/sync`)
}

export function listMcpTools(serverId = 0): Promise<McpTool[]> {
  return get<McpTool[]>(`${BASE}/tools`, { server_id: serverId })
}

export function updateMcpTool(
  id: number,
  data: Partial<{ display_name: string; description: string; risk_level: McpRisk; enabled: boolean }>,
): Promise<McpTool> {
  return put<McpTool>(`${BASE}/tools/${id}`, data)
}

export function listMcpBindings(agentCode = ''): Promise<McpBinding[]> {
  return get<McpBinding[]>(`${BASE}/bindings`, { agent_code: agentCode })
}

export function upsertMcpBinding(data: {
  agent_code: string
  tool_id: number
  permission: McpBinding['permission']
  requires_approval: boolean
  enabled: boolean
}): Promise<McpBinding> {
  return put<McpBinding>(`${BASE}/bindings`, data)
}

export function deleteMcpBinding(id: number): Promise<void> {
  return del<void>(`${BASE}/bindings/${id}`)
}

export function listCapabilityAliases(capabilityCode = ''): Promise<CapabilityAlias[]> {
  return get<CapabilityAlias[]>(`${BASE}/aliases`, { capability_code: capabilityCode })
}

export function createCapabilityAlias(data: Omit<CapabilityAlias, 'id'>): Promise<CapabilityAlias> {
  return post<CapabilityAlias>(`${BASE}/aliases`, data)
}

export function updateCapabilityAlias(id: number, data: Omit<CapabilityAlias, 'id'>): Promise<CapabilityAlias> {
  return put<CapabilityAlias>(`${BASE}/aliases/${id}`, data)
}

export function deleteCapabilityAlias(id: number): Promise<void> {
  return del<void>(`${BASE}/aliases/${id}`)
}

export function listSandboxWorkers(): Promise<SandboxWorker[]> {
  return get<SandboxWorker[]>('/sandboxes/workers')
}

export function createSandboxWorker(data: SandboxWorkerInput): Promise<SandboxWorker> {
  return post<SandboxWorker>('/sandboxes/workers', data)
}

export function updateSandboxWorker(id: number, data: SandboxWorkerInput): Promise<SandboxWorker> {
  return put<SandboxWorker>(`/sandboxes/workers/${id}`, data)
}

export function checkSandboxWorker(id: number): Promise<SandboxWorker> {
  return post<SandboxWorker>(`/sandboxes/workers/${id}/health`)
}

export function seedProductionFallbackWorker(): Promise<SandboxWorker> {
  return post<SandboxWorker>('/sandboxes/workers/seed-production')
}
