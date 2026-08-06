import { download, get, post } from './http'
import type {
  CapabilitySearchResult,
  SandboxCreateInput,
  SandboxEnvironment,
} from '@/types/sandbox'

export function listSandboxes(limit = 50): Promise<SandboxEnvironment[]> {
  return get<SandboxEnvironment[]>('/sandboxes', { limit })
}

export function createSandbox(data: SandboxCreateInput): Promise<SandboxEnvironment> {
  return post<SandboxEnvironment>('/sandboxes', data)
}

export function getSandbox(publicId: string): Promise<SandboxEnvironment> {
  return get<SandboxEnvironment>(`/sandboxes/${encodeURIComponent(publicId)}`)
}

export function stopSandbox(publicId: string): Promise<SandboxEnvironment> {
  return post<SandboxEnvironment>(`/sandboxes/${encodeURIComponent(publicId)}/stop`)
}

export function extendSandbox(publicId: string, hours: number): Promise<SandboxEnvironment> {
  return post<SandboxEnvironment>(`/sandboxes/${encodeURIComponent(publicId)}/extend`, { hours })
}

export function createSandboxPreviewSession(publicId: string): Promise<{ path?: string; preview_path?: string; max_age?: number }> {
  return post<{ path?: string; preview_path?: string; max_age?: number }>(`/sandboxes/${encodeURIComponent(publicId)}/preview-session`)
}

export function downloadSandboxArtifact(publicId: string, artifactId: number): Promise<Blob> {
  return download(`/sandboxes/${encodeURIComponent(publicId)}/artifacts/${artifactId}`)
}

export function searchSandboxCapabilities(q: string, limit = 8): Promise<CapabilitySearchResult[]> {
  return get<CapabilitySearchResult[]>('/sandboxes/capabilities/search', { q, limit })
}
