import { get, post, put, del } from './http'
import type { Page } from '@/types/common'
import type { ProjectOut, ProjectDetailOut } from '@/types/project'

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
 * @returns 更新后的项目
 */
export function updateProject(projectId: number, data: Record<string, unknown>): Promise<ProjectOut> {
  return put<ProjectOut>(`/projects/${projectId}`, data)
}

/**
 * 删除项目
 * @param projectId - 项目 ID
 */
export function deleteProject(projectId: number): Promise<void> {
  return del<void>(`/projects/${projectId}`)
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
