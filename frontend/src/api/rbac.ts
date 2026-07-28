/**
 * RBAC 权限管理 API 封装
 * 对接后端 T10 RBAC 模块,路径前缀 /api/rbac
 * 共封装 15 个接口,涵盖用户角色/权限/菜单/数据范围查询、角色 CRUD、权限分配等
 */

import { get, post, put, del } from './http'
import type {
  Role,
  Permission,
  Menu,
  DataScope,
  UserRoleItem,
  RoleUserItem,
  UserRoleAssignIn,
  RoleCreateIn,
  RoleUpdateIn,
  RolePermissionAssignIn,
  DataScopeUpdateIn,
} from '@/types/rbac'

const BASE = '/rbac'

/* ------------------------------------------------------------------ */
/* 用户维度:角色/权限/菜单/数据范围                                    */
/* ------------------------------------------------------------------ */

/**
 * 分配用户角色
 * @param userId - 用户 ID
 * @param body - 角色分配入参(角色 ID 列表)
 * @returns 用户当前的角色列表
 */
export function assignUserRoles(userId: number, body: UserRoleAssignIn): Promise<UserRoleItem[]> {
  return post<UserRoleItem[]>(`${BASE}/users/${userId}/roles`, body)
}

/**
 * 查询用户角色
 * @param userId - 用户 ID
 * @returns 用户拥有的角色列表
 */
export function fetchUserRoles(userId: number): Promise<UserRoleItem[]> {
  return get<UserRoleItem[]>(`${BASE}/users/${userId}/roles`)
}

/**
 * 查询用户权限点
 * @param userId - 用户 ID
 * @returns 用户拥有的权限编码字符串列表(后端返回 List[str],非对象)
 */
export function fetchUserPermissions(userId: number): Promise<string[]> {
  return get<string[]>(`${BASE}/users/${userId}/permissions`)
}

/**
 * 查询用户菜单(树形)
 * @param userId - 用户 ID
 * @returns 用户可见的菜单树
 */
export function fetchUserMenus(userId: number): Promise<Menu[]> {
  return get<Menu[]>(`${BASE}/users/${userId}/menus`)
}

/**
 * 查询用户数据范围
 * @param userId - 用户 ID
 * @returns 用户的数据范围配置(后端聚合多角色后返回单个对象)
 */
export function fetchUserDataScope(userId: number): Promise<DataScope> {
  return get<DataScope>(`${BASE}/users/${userId}/data-scope`)
}

/* ------------------------------------------------------------------ */
/* 角色 CRUD 与权限/数据范围                                           */
/* ------------------------------------------------------------------ */

/**
 * 列出全部角色
 * @returns 角色列表
 */
export function listRoles(): Promise<Role[]> {
  return get<Role[]>(`${BASE}/roles`)
}

/**
 * 创建角色
 * @param body - 角色创建入参
 * @returns 创建后的角色
 */
export function createRole(body: RoleCreateIn): Promise<Role> {
  return post<Role>(`${BASE}/roles`, body)
}

/**
 * 更新角色
 * @param roleId - 角色 ID
 * @param body - 角色更新入参
 * @returns 更新后的角色
 */
export function updateRole(roleId: number, body: RoleUpdateIn): Promise<Role> {
  return put<Role>(`${BASE}/roles/${roleId}`, body)
}

/**
 * 删除角色
 * @param roleId - 角色 ID
 * @returns void
 */
export function deleteRole(roleId: number): Promise<void> {
  return del<void>(`${BASE}/roles/${roleId}`)
}

/**
 * 查询角色权限
 * @param roleId - 角色 ID
 * @returns 角色拥有的权限点列表
 */
export function fetchRolePermissions(roleId: number): Promise<Permission[]> {
  return get<Permission[]>(`${BASE}/roles/${roleId}/permissions`)
}

/**
 * 分配角色权限
 * @param roleId - 角色 ID
 * @param body - 权限分配入参(权限点 ID 列表)
 * @returns 更新后的角色权限点列表
 */
export function assignRolePermissions(
  roleId: number,
  body: RolePermissionAssignIn,
): Promise<Permission[]> {
  return put<Permission[]>(`${BASE}/roles/${roleId}/permissions`, body)
}

/**
 * 更新角色数据范围
 * @param roleId - 角色 ID
 * @param body - 数据范围更新入参
 * @returns 更新后的数据范围
 */
export function updateRoleDataScope(roleId: number, body: DataScopeUpdateIn): Promise<DataScope> {
  return put<DataScope>(`${BASE}/roles/${roleId}/data-scope`, body)
}

/* ------------------------------------------------------------------ */
/* 权限点 / 菜单 / 按角色查用户                                        */
/* ------------------------------------------------------------------ */

/**
 * 列出全部权限点
 * @returns 权限点列表
 */
export function listPermissions(): Promise<Permission[]> {
  return get<Permission[]>(`${BASE}/permissions`)
}

/**
 * 列出全部菜单(树形)
 * @returns 菜单树
 */
export function listMenus(): Promise<Menu[]> {
  return get<Menu[]>(`${BASE}/menus`)
}

/**
 * 按角色编码查询用户
 * @param roleCode - 角色编码
 * @returns 拥有该角色的用户列表
 */
export function fetchUsersByRole(roleCode: string): Promise<RoleUserItem[]> {
  return get<RoleUserItem[]>(`${BASE}/roles/${encodeURIComponent(roleCode)}/users`)
}
