import { get, post, del, put } from './http'
import type { Page } from '@/types/common'

export interface KnowledgeDoc {
  id: number
  title: string
  source_type: string
  source_ref?: string | null
  char_count: number
  chunk_count: number
  create_time: string
}

export interface SearchHit {
  content: string
  score: number
  doc_id: number
  title: string
  source_type: string
}

export interface KbStats {
  doc_total: number
  chunk_total: number
  by_source: Record<string, number>
  remote_embedding: boolean
}

export interface SyncResult {
  code: number
  issue: number
  forum: number
  feedback: number
  ticket: number
  total: number
}

export interface EmbeddingConfig {
  base_url: string
  model: string
  enabled: boolean
  api_key_set: boolean
}

/** 知识库文档列表 */
export function getDocs(params?: Record<string, unknown>): Promise<Page<KnowledgeDoc>> {
  return get<Page<KnowledgeDoc>>('/knowledge/docs', params)
}

/** 添加知识文档 */
export function addDoc(data: { title: string; content: string }): Promise<{ id: number; chunk_count: number }> {
  return post<{ id: number; chunk_count: number }>('/knowledge/docs', data)
}

/** 删除知识文档 */
export function deleteDoc(id: number): Promise<void> {
  return del<void>(`/knowledge/docs/${id}`)
}

/** 检索 */
export function searchKnowledge(data: { query: string; top_k?: number }): Promise<SearchHit[]> {
  return post<SearchHit[]>('/knowledge/search', data)
}

/** 从平台数据同步 */
export function syncKnowledge(): Promise<SyncResult> {
  return post<SyncResult>('/knowledge/sync')
}

/** 统计 */
export function getKbStats(): Promise<KbStats> {
  return get<KbStats>('/knowledge/stats')
}

/** 一键重建全部存量切片向量(个人KB+Agent知识库, 唯一超管) */
export function reembedAll() {
  return post<Record<string, number>>('/knowledge/embedding-config/reembed', {})
}

/** 获取 embedding 配置 (管理员) */
export function getEmbeddingConfig(): Promise<EmbeddingConfig> {
  return get<EmbeddingConfig>('/knowledge/embedding-config')
}

/** 更新 embedding 配置 (管理员) */
export function updateEmbeddingConfig(data: Partial<{
  base_url: string; model: string; api_key: string; enabled: boolean
}>): Promise<EmbeddingConfig> {
  return put<EmbeddingConfig>('/knowledge/embedding-config', data)
}
