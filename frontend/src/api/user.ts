import { get, post, del } from './http'
import type { Page } from '@/types/common'
import type { UserListItem } from '@/types/user'

export function getUsers(params?: Record<string, unknown>, token?: string | null) {
  return get<Page<UserListItem>>('/users', params, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export function resetPassword(userId: number) {
  return post<{ temporary_password: string }>(`/users/${userId}/reset-password`)
}

export function toggleUserStatus(userId: number, status: number) {
  return post<null>(`/users/${userId}/toggle-status`, { status })
}

export function deleteUser(userId: number) {
  return del<null>(`/users/${userId}`)
}
