import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { UserOut, LoginIn, RegisterIn } from '@/types/auth'
import { login as authLogin, register as authRegister, me as authMe } from '@/api/auth'
import { setToken, clearToken, getToken } from '@/utils/token'

let authExpiredListenerRegistered = false

/**
 * 用户状态管理 Store，管理认证状态、用户信息与 Token 持久化
 */
export const useUserStore = defineStore('user', () => {
  const token = ref<string>(getToken() || '')
  const profile = ref<UserOut | null>(null)

  const isLoggedIn = computed(() => !!token.value && !!profile.value)
  const displayName = computed(() => profile.value?.nickname || profile.value?.username || '')

  /**
   * 用户登录，保存 token 并获取用户信息
   * @param data 登录请求参数
   */
  async function login(data: LoginIn) {
    const res = await authLogin(data)
    token.value = res.access_token
    setToken(res.access_token)
    profile.value = res.user
  }

  /**
   * 用户注册
   * @param data 注册请求参数
   */
  async function register(data: RegisterIn) {
    await authRegister(data)
  }

  /**
   * 获取当前用户信息，用于从已有 Token 恢复会话
   */
  async function fetchProfile() {
    profile.value = await authMe()
  }

  /**
   * 退出登录，清除本地状态
   * @returns void
   */
  function logout(): void {
    token.value = ''
    profile.value = null
    clearToken()
  }

  /**
   * 同步认证过期后的本地状态
   * @returns void
   */
  function syncAuthExpiredState(): void {
    token.value = ''
    profile.value = null
  }

  /**
   * 注册全局认证过期监听，保证拦截器清 token 后 Pinia 状态同步
   * @returns void
   */
  function registerAuthExpiredListener(): void {
    if (authExpiredListenerRegistered) return
    window.addEventListener('prism:auth-expired', syncAuthExpiredState)
    authExpiredListenerRegistered = true
  }

  registerAuthExpiredListener()

  return {
    token,
    profile,
    isLoggedIn,
    displayName,
    login,
    register,
    fetchProfile,
    logout,
  }
})
