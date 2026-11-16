import { defineStore, getActivePinia } from 'pinia'
import { ref } from 'vue'
import { hasEnterpriseMembership as requestEnterpriseMembership } from '@/services/enterpriseMembership'

export type WorkspaceMode = 'personal' | 'enterprise'

/** Agent 导航等非组件代码读取当前 Pinia 空间；没有应用上下文的纯单测默认个人模式。 */
export function getActiveWorkspaceMode(): WorkspaceMode {
  const pinia = getActivePinia()
  return pinia ? useWorkspaceStore(pinia).activeSpace : 'personal'
}

interface SavedWorkspace {
  identity: string
  mode: WorkspaceMode
  enterpriseId: number | null
}

const STORAGE_KEY = 'prism.workspace-context.v1'

/**
 * 返回仅用于识别当前登录会话的短指纹；不保存认证令牌，也不承担授权用途。
 */
function tokenFingerprint(token: string): string {
  let hash = 2166136261
  for (let index = 0; index < token.length; index += 1) {
    hash ^= token.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function readSavedWorkspace(identity: string): SavedWorkspace | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<SavedWorkspace>
    if (value.identity !== identity || (value.mode !== 'personal' && value.mode !== 'enterprise')) return null
    const enterpriseId = Number.isSafeInteger(value.enterpriseId) && Number(value.enterpriseId) > 0
      ? Number(value.enterpriseId)
      : null
    return { identity, mode: value.mode, enterpriseId }
  } catch {
    return null
  }
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const activeSpace = ref<WorkspaceMode>('personal')
  /** 只用于恢复 UI 选择；企业页面仍必须重新从服务端验证成员关系。 */
  const activeEnterpriseId = ref<number | null>(null)
  /** null 表示尚未向服务端确认；公开论坛入口在确认前隐藏。 */
  const hasEnterpriseMembership = ref<boolean | null>(null)
  let identityStamp: string | null = null
  let identityToken: string | null = null
  let membershipIdentity: string | null = null
  let membershipRequestIdentity: string | null = null
  let membershipRequest: Promise<boolean | null> | null = null
  let membershipRevision = 0

  function persist(): void {
    if (!identityStamp) return
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        identity: identityStamp,
        mode: activeSpace.value,
        enterpriseId: activeEnterpriseId.value,
      } satisfies SavedWorkspace))
    } catch {
      // 存储不可用时仍允许本页正常切换空间。
    }
  }

  /** 账号或认证令牌变化时重置空间；页面刷新时仅恢复同一会话偏好。 */
  function syncIdentity(userId: number | null | undefined, token: string | null | undefined): void {
    if (!userId || !token) {
      identityStamp = null
      identityToken = null
      membershipIdentity = null
      membershipRequestIdentity = null
      membershipRequest = null
      membershipRevision += 1
      hasEnterpriseMembership.value = false
      activeSpace.value = 'personal'
      activeEnterpriseId.value = null
      try { window.sessionStorage.removeItem(STORAGE_KEY) } catch { /* 可选的界面偏好 */ }
      return
    }

    const nextIdentity = `${userId}:${tokenFingerprint(token)}`
    if (identityStamp === nextIdentity) return
    identityStamp = nextIdentity
    identityToken = token
    membershipIdentity = null
    membershipRequestIdentity = null
    membershipRequest = null
    membershipRevision += 1
    hasEnterpriseMembership.value = null
    const saved = readSavedWorkspace(nextIdentity)
    activeSpace.value = saved?.mode ?? 'personal'
    activeEnterpriseId.value = saved?.mode === 'enterprise' ? saved.enterpriseId : null
    if (!saved) persist()
  }

  /** 服务端成员目录决定账号能否访问公开论坛；本地空间选择不参与授权。 */
  async function refreshEnterpriseMembership(): Promise<boolean | null> {
    const requestedIdentity = identityStamp
    const requestedToken = identityToken
    if (!requestedIdentity || !requestedToken) return false
    if (membershipIdentity === requestedIdentity && hasEnterpriseMembership.value !== null) {
      return hasEnterpriseMembership.value
    }
    if (membershipRequestIdentity === requestedIdentity && membershipRequest) return membershipRequest

    membershipRequestIdentity = requestedIdentity
    const requestedRevision = membershipRevision
    const request = requestEnterpriseMembership(requestedToken)
      .then((isMember) => {
        if (identityStamp !== requestedIdentity || membershipRevision !== requestedRevision) return null
        hasEnterpriseMembership.value = isMember
        membershipIdentity = requestedIdentity
        return isMember
      })
      .catch(() => null)
      .finally(() => {
        if (membershipRequestIdentity === requestedIdentity) {
          membershipRequestIdentity = null
          membershipRequest = null
        }
      })
    membershipRequest = request
    return request
  }

  /** 企业页获得新鲜成员目录后同步账号类型提示，并让较早请求失效。 */
  function setEnterpriseMembership(hasMembership: boolean): void {
    if (!identityStamp) return
    membershipRevision += 1
    membershipRequestIdentity = null
    membershipRequest = null
    hasEnterpriseMembership.value = hasMembership
    membershipIdentity = identityStamp
  }

  function activatePersonal(): void {
    activeSpace.value = 'personal'
    activeEnterpriseId.value = null
    persist()
  }

  function activateEnterprise(enterpriseId?: number | null): void {
    activeSpace.value = 'enterprise'
    if (enterpriseId !== undefined) {
      activeEnterpriseId.value = Number.isSafeInteger(enterpriseId) && Number(enterpriseId) > 0
        ? Number(enterpriseId)
        : null
    }
    persist()
  }

  return {
    activeSpace,
    activeEnterpriseId,
    hasEnterpriseMembership,
    syncIdentity,
    refreshEnterpriseMembership,
    setEnterpriseMembership,
    activatePersonal,
    activateEnterprise,
  }
})
