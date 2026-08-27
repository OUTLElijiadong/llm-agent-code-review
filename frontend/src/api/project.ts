import http, { get, post, put, del, download, type Resp } from './http'
import type { Page } from '@/types/common'
import type { ProjectOut, ProjectDetailOut, ProjectSourceArchiveOut } from '@/types/project'
import type { SecurityScanOut } from '@/types/security'

/**
 * 获取项目分页列表
 * @param params - 查询参数 { page, page_size, keyword, language, status }
 * @returns 分页项目列表
 */
export function getProjects(params?: Record<string, unknown>): Promise<Page<ProjectOut>> {
  return get<Page<ProjectOut>>('/projects', params)
}

/**
 * 获取项目详情
 * @param projectId - 项目 ID
 * @returns 项目详情
 */
export function getProjectDetail(projectId: number): Promise<ProjectDetailOut> {
  return get<ProjectDetailOut>(`/projects/${projectId}`)
}

/**
 * 创建项目
 * @param data - 项目创建参数
 * @returns 创建的项目ID
 */
export function createProject(data: Record<string, unknown>): Promise<{ id: number }> {
  return post<{ id: number }>('/projects', data)
}

/**
 * 更新项目
 * @param projectId - 项目 ID
 * @param data - 项目更新参数
 * @returns void
 */
export function updateProject(projectId: number, data: Record<string, unknown>): Promise<void> {
  return put<void>(`/projects/${projectId}`, data)
}

/**
 * 删除项目
 * @param projectId - 项目 ID
 */
export function deleteProject(projectId: number): Promise<void> {
  return del<void>(`/projects/${projectId}`)
}

export interface RemoteProjectImportInput {
  url: string
  project_name: string
  description?: string
  language?: string
  audit_mode?: boolean
}

export type RemoteProjectImportStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface RemoteProjectImportTask {
  task_id: string
  status: RemoteProjectImportStatus
  attempt_count: number
  max_attempts: number
  project_id: number | null
  result: {
    id?: number | string
    file_count?: number
    progress?: {
      phase?: string
      received_bytes?: number
      total_bytes?: number | null
      [key: string]: unknown
    }
    [key: string]: unknown
  }
  error: { code: string; message: string } | null
  next_attempt_at: string | null
  started_at: string | null
  completed_at: string | null
  create_time: string
  update_time: string
}

/**
 * 创建可恢复的远程导入任务。
 *
 * 这里不能使用通用 post helper,因为导入接口依赖请求级
 * Idempotency-Key;直接复用同一个 Axios 实例仍保留鉴权和错误拦截器。
 */
export async function queueRemoteProjectImport(
  data: RemoteProjectImportInput,
  idempotencyKey: string,
): Promise<RemoteProjectImportTask> {
  const response = await http.post<Resp<RemoteProjectImportTask>>(
    '/projects/remote-imports',
    data,
    { headers: { 'Idempotency-Key': idempotencyKey } },
  )
  return response.data.data as RemoteProjectImportTask
}

/** 查询当前用户创建的远程导入任务。 */
export function getRemoteProjectImport(taskId: string): Promise<RemoteProjectImportTask> {
  return get<RemoteProjectImportTask>(`/projects/remote-imports/${encodeURIComponent(taskId)}`)
}

/**
 * 兼容旧调用者的同步导入接口;项目列表等第一方页面应使用异步任务接口。
 * @deprecated 请使用 queueRemoteProjectImport。
 */
export function importRemoteProject(data: RemoteProjectImportInput): Promise<{ id: number; file_count: number }> {
  return post<{ id: number; file_count: number }>('/projects/import-remote', data)
}

/** 上传可能含恶意代码的整包 ZIP，保留原包供白盒审计。 */
export function uploadAuditSourceArchive(
  projectId: number,
  file: File,
): Promise<ProjectSourceArchiveOut> {
  const formData = new FormData()
  formData.append('file', file, file.name)
  return post<ProjectSourceArchiveOut>(`/projects/${projectId}/audit-source-archive`, formData)
}

/** 查询项目的隔离源码包状态。 */
export function getAuditSourceArchive(
  projectId: number,
): Promise<ProjectSourceArchiveOut | null> {
  return get<ProjectSourceArchiveOut | null>(`/projects/${projectId}/audit-source-archive`)
}

/** 读取与当前隔离原包摘要绑定的最近一次持久化审计报告。 */
export function getAuditSourceArchiveResult(projectId: number): Promise<{
  status: string
  started_at?: string | null
  completed_at?: string | null
  result: SecurityScanOut
} | null> {
  return get(`/projects/${projectId}/audit-source-archive/result`)
}

/** 下载项目当前 active 文件组成的源码 ZIP。 */
export function deleteSourceRevision(projectId: number, revisionId: number): Promise<{ deleted: number }> {
  return del<{ deleted: number }>(`/projects/${projectId}/source-revisions/${revisionId}`)
}

export function downloadProjectSource(projectId: number): Promise<Blob> {
  return download(`/projects/${projectId}/source-archive`)
}

/**
 * AI智能检测项目语言
 * @param data - { project_name, description }
 * @returns 检测结果 { language, language_name, confidence, reason }
 */
export function detectLanguage(data: { project_name: string; description?: string }): Promise<{
  language: string
  language_name: string
  confidence: string
  reason: string
}> {
  return post('/ai/detect-language', data)
}

/**
 * AI智能分析文件夹，生成项目元数据
 * @param data - { folder_name, file_names }
 * @returns 项目元数据 { project_name, description, language, language_name }
 */
export function analyzeFolder(data: { folder_name: string; file_names: string[] }): Promise<{
  project_name: string
  description: string
  language: string
  language_name: string
}> {
  return post('/ai/analyze-folder', data)
}
