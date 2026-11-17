import { getActivePinia } from 'pinia'
import { onBeforeUnmount, watch } from 'vue'
import type { Router } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'

/** 导航延迟执行时只能沿用发起时的账号、权限、空间及聊天会话。 */
export function useXiaolingNavigationGuard(router: Router, session: () => string = () => '') {
  const user = useUserStore()
  const pinia = getActivePinia()
  const workspace = pinia ? useWorkspaceStore(pinia) : null
  let epoch = 0
  let disposed = false
  watch(() => [user.token, user.profile?.id, user.profile?.role, user.profile?.status,
    [...(user.roles || [])].sort().join('\u0000'), [...(user.permissions || [])].sort().join('\u0000'),
    workspace?.activeSpace, workspace?.activeEnterpriseId, workspace?.hasEnterpriseMembership, session()],
  () => { epoch += 1 }, { flush: 'sync' })
  onBeforeUnmount(() => { disposed = true; epoch += 1 })

  return (route: string): (() => boolean) => {
    const captured = epoch
    const userId = user.profile?.id
    const token = user.token
    return () => !disposed && captured === epoch && !!token && typeof userId === 'number' && userId > 0
      && isNavigationPathAllowed(router, route, user)
  }
}
