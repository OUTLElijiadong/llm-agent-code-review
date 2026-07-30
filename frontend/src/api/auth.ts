import { post, get } from './http'
import type { LoginIn, RegisterIn, LoginOut, UserOut, ChangePasswordIn } from '@/types/auth'

/**
 * 用户登录
 * @param body 登录参数（用户名、密码）
 * @returns 登录令牌及用户信息
 */
export function login(body: LoginIn): Promise<LoginOut> {
  return post<LoginOut>('/auth/login', body)
}

/**
 * 用户注册
 * @param body 注册参数（用户名、密码、邮箱、昵称）
 * @returns 注册成功后的用户信息
 */
export function register(body: RegisterIn): Promise<{ user_id: number; username: string }> {
  return post<{ user_id: number; username: string }>('/auth/register', body)
}

/**
 * 获取注册验证码(数学题)
 * @returns captcha_id 与题目
 */
export function getCaptcha(): Promise<{ captcha_id: string; question: string }> {
  return get<{ captcha_id: string; question: string }>('/auth/captcha')
}

/**
 * 获取当前登录用户信息
 * @returns 当前用户详情
 */
export function me(): Promise<UserOut> {
  return get<UserOut>('/auth/me')
}

/**
 * 用户登出
 * @returns void
 */
export function logout(): Promise<void> {
  return post<void>('/auth/logout')
}

/**
 * 修改当前用户密码
 * @param body 新旧密码
 * @returns void
 */
export function changePassword(body: ChangePasswordIn): Promise<void> {
  return post<void>('/auth/change-password', body)
}
