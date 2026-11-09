import { del, get, post } from './http'

export interface EnterpriseAssetDraft {
  id: number
  enterprise_id: number
  creator_id: number
  kind: 'agent' | 'skill'
  title: string
  definition: Record<string, unknown>
  current_version: number
  status: string
  published_scopes: Array<'enterprise' | 'global'>
  sharing_available: boolean
  execution_available: boolean
}
export interface EnterpriseAssetVersion {
  id: number
  draft_id: number
  version_number: number
  title: string
  definition: Record<string, unknown>
}
export interface EnterpriseAssetPublication {
  id: number
  enterprise_id?: number
  draft_id: number
  version_id: number
  version_number: number
  requested_by: number
  kind: 'agent' | 'skill'
  scope: 'enterprise' | 'global'
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'revoked'
  title: string
  definition: Record<string, unknown>
  content_sha256: string
  execution_available: false
  xiaoling_runtime_config_compatible: boolean
  decided_by: number | null
  decided_at: string | null
  decision_reason: string
  revoked_by: number | null
  revoked_at: string | null
  revocation_reason: string
}
export interface EnterpriseAssetCatalogItem {
  id: number
  version_number: number
  kind: 'agent' | 'skill'
  scope: 'enterprise' | 'global'
  title: string
  definition: Record<string, unknown>
  content_sha256: string
  execution_available: false
  xiaoling_runtime_config_compatible: boolean
  personal_execution_available?: boolean
  can_revoke: boolean
}
type DraftInput = { title: string; definition: Record<string, unknown>; request_key: string }
const sessionOptions = (token?: string | null) =>
  token === undefined ? undefined : { authTokenSnapshot: token }

export function listEnterpriseAssetDrafts(enterpriseId: number, offset = 0, token?: string | null) {
  return get<EnterpriseAssetDraft[]>(`/enterprises/${enterpriseId}/asset-drafts`, { offset, limit: 100 },
    undefined, false, sessionOptions(token))
}
export function getEnterpriseAssetDraft(enterpriseId: number, id: number, token?: string | null) {
  return get<EnterpriseAssetDraft>(`/enterprises/${enterpriseId}/asset-drafts/${id}`, undefined, undefined, false,
    sessionOptions(token))
}
export function createEnterpriseAssetDraft(enterpriseId: number, input: DraftInput & { kind: 'agent' | 'skill' },
                                           token?: string | null) {
  return post<EnterpriseAssetDraft>(`/enterprises/${enterpriseId}/asset-drafts`, input, undefined, undefined, false,
    sessionOptions(token))
}
export function reviseEnterpriseAssetDraft(enterpriseId: number, id: number,
                                           input: DraftInput & { expected_version: number }, token?: string | null) {
  return post<EnterpriseAssetVersion>(`/enterprises/${enterpriseId}/asset-drafts/${id}/versions`, input,
    undefined, undefined, false, sessionOptions(token))
}
export function deleteEnterpriseAssetDraft(enterpriseId: number, id: number, token?: string | null) {
  return del<null>(`/enterprises/${enterpriseId}/asset-drafts/${id}`, sessionOptions(token))
}

export function requestEnterpriseAssetPublication(enterpriseId: number, draftId: number,
                                                   input: { scope: 'enterprise' | 'global';
                                                     expected_version: number; request_key: string },
                                                   token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/enterprises/${enterpriseId}/asset-drafts/${draftId}/publication-requests`, input,
    undefined, undefined, false, sessionOptions(token),
  )
}
export function listMyEnterpriseAssetPublications(enterpriseId: number, token?: string | null) {
  return get<EnterpriseAssetPublication[]>(`/enterprises/${enterpriseId}/asset-publications/mine`,
    { offset: 0, limit: 100 }, undefined, false, sessionOptions(token))
}
export function withdrawEnterpriseAssetPublication(enterpriseId: number, publicationId: number,
                                                    token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/enterprises/${enterpriseId}/asset-publications/${publicationId}/withdraw`, {},
    undefined, undefined, false, sessionOptions(token),
  )
}
export function listPendingEnterpriseAssetPublications(enterpriseId: number, token?: string | null) {
  return get<EnterpriseAssetPublication[]>(`/enterprises/${enterpriseId}/asset-publications/pending`,
    { offset: 0, limit: 100 }, undefined, false, sessionOptions(token))
}
export function decideEnterpriseAssetPublication(enterpriseId: number, publicationId: number,
                                                  input: { decision: 'approve' | 'reject'; reason: string },
                                                  token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/enterprises/${enterpriseId}/asset-publications/${publicationId}/decision`, input,
    undefined, undefined, false, sessionOptions(token),
  )
}
export function revokeEnterpriseAssetPublication(enterpriseId: number, publicationId: number,
                                                  input: { reason: string }, token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/enterprises/${enterpriseId}/asset-publications/${publicationId}/revoke`, input,
    undefined, undefined, false, sessionOptions(token),
  )
}
export function listEnterpriseAssetCatalog(enterpriseId: number, token?: string | null) {
  return get<EnterpriseAssetCatalogItem[]>(`/enterprises/${enterpriseId}/asset-catalog`,
    { offset: 0, limit: 100 }, undefined, false, sessionOptions(token))
}
export function listGlobalEnterpriseAssetCatalog(token?: string | null, beforeId?: number) {
  return get<EnterpriseAssetCatalogItem[]>('/enterprise-asset-catalog',
    { offset: 0, limit: 100, ...(beforeId === undefined ? {} : { before_id: beforeId }) },
    undefined, false, sessionOptions(token))
}
export function listPlatformEnterpriseAssetPublications(status: 'pending' | 'approved' | 'rejected' | 'revoked',
                                                        token?: string | null) {
  return get<EnterpriseAssetPublication[]>('/admin/enterprise-asset-publications',
    { status, offset: 0, limit: 100 }, undefined, false, sessionOptions(token))
}
export function decidePlatformEnterpriseAssetPublication(publicationId: number,
                                                          input: { decision: 'approve' | 'reject'; reason: string },
                                                          token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/admin/enterprise-asset-publications/${publicationId}/decision`, input,
    undefined, undefined, false, sessionOptions(token),
  )
}
export function revokePlatformEnterpriseAssetPublication(publicationId: number,
                                                          input: { reason: string }, token?: string | null) {
  return post<EnterpriseAssetPublication>(
    `/admin/enterprise-asset-publications/${publicationId}/revoke`, input,
    undefined, undefined, false, sessionOptions(token),
  )
}
