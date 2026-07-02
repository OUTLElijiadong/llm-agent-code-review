/**
 * RBAC 权限模型类型定义
 * 与后端 T10 RBAC schema 对齐,涵盖角色/权限点/菜单/数据范围
 */

/**
 * 权限点定义
 */
export interface Permission {
  /** 权限点 ID */
  id: number
  /** 权限点编码,格式为 module:action,例如 project:view */
  code: string
  /** 权限点名称 */
  name: string
  /** 所属模块: project/file/review/issue/agent/report/audit/user/role/menu */
  module: string
  /** 权限点描述 */
  description?: string
}

/**
 * 角色定义
 */
export interface Role {
  /** 角色 ID */
  id: number
  /** 角色编码,例如 admin/reviewer/user */
  code: string
  /** 角色名称 */
  name: string
  /** 角色描述 */
  description?: string
  /** 是否为内置角色(内置角色不允许删除) */
  is_builtin: boolean
  /** 角色拥有的权限点列表(查询角色权限时返回) */
  permissions?: Permission[]
}

/**
 * 菜单定义(树形结构)
 */
export interface Menu {
  /** 菜单 ID */
  id: number
  /** 父菜单 ID,根菜单为 0 或 null */
  parent_id: number | null
  /** 菜单编码 */
  code: string
  /** 菜单名称 */
  name: string
  /** 菜单路由路径 */
  path: string
  /** 菜单对应组件路径 */
  component?: string
  /** 菜单图标 */
  icon?: string
  /** 排序序号 */
  sort_order: number
  /** 是否可见 */
  is_visible: boolean
  /** 子菜单列表 */
  children?: Menu[]
}

/**
 * 数据范围类型
 * - all: 全部数据
 * - project: 指定项目
 * - self: 仅本人数据
 */
export type DataScopeType = 'all' | 'project' | 'self'

/**
 * 数据范围定义
 */
export interface DataScope {
  /** 数据范围 ID */
  id: number
  /** 角色 ID */
  role_id: number
  /** 范围类型: all/project/self */
  scope_type: DataScopeType
  /** 项目 ID 列表(scope_type 为 project 时有效) */
  project_ids?: number[]
}

/**
 * 用户角色分配入参
 */
export interface UserRoleAssignIn {
  /** 用户 ID */
  user_id: number
  /** 角色 ID 列表 */
  role_ids: number[]
}

/**
 * 角色创建入参
 */
export interface RoleCreateIn {
  /** 角色编码 */
  code: string
  /** 角色名称 */
  name: string
  /** 角色描述 */
  description?: string
}

/**
 * 角色更新入参
 */
export interface RoleUpdateIn {
  /** 角色名称 */
  name?: string
  /** 角色描述 */
  description?: string
}

/**
 * 角色权限分配入参
 */
export interface RolePermissionAssignIn {
  /** 权限点 ID 列表 */
  permission_ids: number[]
}

/**
 * 数据范围更新入参
 */
export interface DataScopeUpdateIn {
  /** 范围类型: all/project/self */
  scope_type: DataScopeType
  /** 项目 ID 列表(scope_type 为 project 时必填) */
  project_ids?: number[]
}

/**
 * 用户角色项(查询用户角色返回)
 */
export interface UserRoleItem {
  /** 角色 ID */
  id: number
  /** 角色编码 */
  code: string
  /** 角色名称 */
  name: string
  /** 是否为内置角色 */
  is_builtin: boolean
}

/**
 * 按角色查询用户返回的用户项
 */
export interface RoleUserItem {
  /** 用户 ID */
  id: number
  /** 用户名 */
  username: string
  /** 昵称 */
  nickname?: string
  /** 邮箱 */
  email?: string
  /** 账号状态 */
  status: number
}
