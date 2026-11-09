import { del, download as downloadBlob, get, post, put } from './http'

export interface EnterpriseKnowledgeEntry {
  id: number
  enterprise_id: number
  author_id: number
  updated_by: number
  version: number
  title: string
  content?: string
  create_time: string
  update_time: string
}
export interface EnterpriseKnowledgeAttachment {
  id: number
  enterprise_id: number
  author_id: number
  updated_by: number
  file_name: string
  file_ext: string
  mime: string
  file_size: number
  chunk_count: number
  version: number
  create_time: string
  update_time: string
}
export interface EnterpriseKnowledgeAttachmentHit {
  attachment_id: number
  version: number
  file_name: string
  excerpt: string
  score: number
  search_mode: 'local_hash_vector_and_terms'
}
const base = '/enterprises'
const sessionOptions = (token: string | null) => ({ authTokenSnapshot: token })
export function listEnterpriseKnowledge(id: number, query = '', beforeId?: number) {
  const pagination = { limit: 30, ...(beforeId ? { before_id: beforeId } : {}) }
  if (query) {
    return get<{ items: EnterpriseKnowledgeEntry[] }>(`${base}/${id}/knowledge/search`,
      { ...pagination, q: query }, [40300, 40301, 40303, 40400])
  }
  return get<{ items: EnterpriseKnowledgeEntry[] }>(`${base}/${id}/knowledge/entries`, pagination,
    [40300, 40301, 40303, 40400])
}
export function getEnterpriseKnowledge(id: number, entry: number) {
  return get<EnterpriseKnowledgeEntry>(`${base}/${id}/knowledge/entries/${entry}`)
}
export function createEnterpriseKnowledge(id: number, title: string, content: string, key: string, token: string | null) {
  return post<EnterpriseKnowledgeEntry>(`${base}/${id}/knowledge/entries`, { title, content, client_request_id: key },
    undefined, undefined, false, { authTokenSnapshot: token })
}
export function updateEnterpriseKnowledge(id: number, entry: number, title: string, content: string,
                                          expectedVersion: number, token: string | null) {
  return put<EnterpriseKnowledgeEntry>(`${base}/${id}/knowledge/entries/${entry}`,
    { title, content, expected_version: expectedVersion }, { authTokenSnapshot: token })
}
export function deleteEnterpriseKnowledge(id: number, entry: number, expectedVersion: number, token: string | null) {
  return del<null>(`${base}/${id}/knowledge/entries/${entry}?expected_version=${expectedVersion}`, { authTokenSnapshot: token })
}
export function listEnterpriseKnowledgeAttachments(id: number, beforeId: number | undefined, token: string | null) {
  return get<{ items: EnterpriseKnowledgeAttachment[] }>(`${base}/${id}/knowledge/attachments`,
    { limit: 30, ...(beforeId ? { before_id: beforeId } : {}) }, undefined, false, sessionOptions(token))
}
export function searchEnterpriseKnowledgeAttachments(id: number, query: string, token: string | null) {
  return get<{ items: EnterpriseKnowledgeAttachmentHit[]; search_mode: string }>(
    `${base}/${id}/knowledge/attachments/search`, { q: query, limit: 5 }, undefined, false, sessionOptions(token))
}
export function uploadEnterpriseKnowledgeAttachment(id: number, file: File, requestId: string, token: string | null) {
  const form = new FormData()
  form.append('file', file)
  form.append('client_request_id', requestId)
  return post<EnterpriseKnowledgeAttachment>(`${base}/${id}/knowledge/attachments`, form,
    undefined, undefined, false, sessionOptions(token))
}
export function deleteEnterpriseKnowledgeAttachment(id: number, attachmentId: number, expectedVersion: number,
                                                    token: string | null) {
  return del<null>(`${base}/${id}/knowledge/attachments/${attachmentId}?expected_version=${expectedVersion}`,
    sessionOptions(token))
}
export function downloadEnterpriseKnowledgeAttachment(id: number, attachmentId: number, token: string | null) {
  return downloadBlob(`${base}/${id}/knowledge/attachments/${attachmentId}/download`, undefined,
    sessionOptions(token))
}
