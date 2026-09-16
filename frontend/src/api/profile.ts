import { get, put, post } from './http'

export interface UserProfile {
  user_id: number
  hobbies: string
  goals: string
  tech_stack: string
  focus_areas: string[]
  preferred_language: string
  experience_level: string
  auto_learn: boolean
  derived_summary: string
  derived_stats: Record<string, unknown>
  last_learned_at?: string | null
  should_prompt?: boolean
  preference_prompted?: number
  preference_prompted_at?: string | null
  update_time?: string | null
}

/** 获取本人画像 */
export function getProfile(): Promise<UserProfile> {
  return get<UserProfile>('/me/profile')
}

/** 更新本人显式画像 */
export function updateProfile(data: Partial<{
  hobbies: string
  goals: string
  tech_stack: string
  focus_areas: string[]
  preferred_language: string
  experience_level: string
  auto_learn: boolean
  preference_prompted: 1 | 2
  clear_learned: boolean
}>): Promise<UserProfile> {
  return put<UserProfile>('/me/profile', data)
}

/** 触发隐式学习(从行为重新推断画像) */
export function relearnProfile(): Promise<UserProfile> {
  return post<UserProfile>('/me/profile/relearn')
}

/** 上报小菱偏好询问结果(1=已答 2=跳过) */
export function markPreferencePrompted(state: 1 | 2): Promise<UserProfile> {
  return post<UserProfile>('/me/profile/preference-prompted', { state })
}
