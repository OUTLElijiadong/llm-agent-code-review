import { get, post, del } from './http'
import type { Page } from '@/types/common'
import type { UserListItem } from '@/types/user'

export function getUsers(params?: Record<string, unknown>) {
  return get<Page<UserListItem>>('/users', params)
}

export function resetPassword(userId: number) {
  return post<{ password: string }>(`/users/${userId}/reset-password`)
}

export function toggleUserStatus(userId: number, status: number) {
  return post<null>(`/users/${userId}/toggle-status`, { status })
}

export function setUserRole(userId: number, role: string) {
  return post<null>(`/users/${userId}/role`, { role })
}

export function deleteUser(userId: number) {
  return del<null>(`/users/${userId}`)
}
