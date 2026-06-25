/**
 * 项目成员类型定义
 * 对应后端 schemas/project_member.py 的 MemberOut / MemberAddIn / MemberRoleUpdateIn
 */

/** 项目内角色枚举（与后端 pattern ^(owner|reviewer)$ 对齐） */
export type ProjectRole = 'owner' | 'reviewer'

/** 项目成员响应项（对应后端 MemberOut） */
export interface ProjectMemberOut {
  /** 成员记录 ID */
  id: number
  /** 关联用户 ID */
  user_id: number
  /** 用户登录名 */
  username: string
  /** 用户昵称（可为空） */
  nickname?: string | null
  /** 项目内角色 */
  role_in_project: ProjectRole
  /** 加入时间 */
  create_time: string
}

/** 添加成员请求体（对应后端 MemberAddIn） */
export interface ProjectMemberAddIn {
  /** 被加入的用户 ID */
  user_id: number
  /** 项目内角色，默认 reviewer */
  role_in_project?: ProjectRole
}

/** 更新成员角色请求体（对应后端 MemberRoleUpdateIn） */
export interface ProjectMemberRoleUpdateIn {
  /** 新的项目内角色 */
  role_in_project: ProjectRole
}
