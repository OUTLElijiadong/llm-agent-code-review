/**
 * 项目成员管理 API 封装
 * 对应后端路由前缀 /api/projects/{project_id}/members
 */
import { get, post, put, del } from './http'
import type {
  ProjectMemberOut,
  ProjectMemberAddIn,
  ProjectMemberRoleUpdateIn,
} from '@/types/projectMember'

/**
 * 获取项目成员列表
 * @param projectId - 项目 ID
 * @returns 成员列表
 */
export function listProjectMembers(projectId: number): Promise<ProjectMemberOut[]> {
  return get<ProjectMemberOut[]>(`/projects/${projectId}/members`)
}

/**
 * 添加项目成员
 * @param projectId - 项目 ID
 * @param data - 添加成员请求体（含 user_id 和 role_in_project）
 * @returns 新增成员信息
 */
export function addProjectMember(
  projectId: number,
  data: ProjectMemberAddIn,
): Promise<ProjectMemberOut> {
  return post<ProjectMemberOut>(`/projects/${projectId}/members`, data)
}

/**
 * 更新项目成员角色
 * @param projectId - 项目 ID
 * @param userId - 被更新用户的 ID
 * @param data - 角色更新请求体（含新 role_in_project）
 */
export function updateProjectMemberRole(
  projectId: number,
  userId: number,
  data: ProjectMemberRoleUpdateIn,
): Promise<void> {
  return put<void>(`/projects/${projectId}/members/${userId}`, data)
}

/**
 * 移除项目成员
 * @param projectId - 项目 ID
 * @param userId - 被移除用户的 ID
 */
export function removeProjectMember(projectId: number, userId: number): Promise<void> {
  return del<void>(`/projects/${projectId}/members/${userId}`)
}
