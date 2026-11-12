import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useWorkspaceStore } from './workspace'

describe('全站工作空间上下文', () => {
  beforeEach(() => {
    sessionStorage.clear()
    setActivePinia(createPinia())
  })

  it('同一登录会话刷新后恢复空间偏好，但企业 ID 仅是待服务端复核的选择', () => {
    const first = useWorkspaceStore()
    first.syncIdentity(17, 'account-token-a')
    first.activateEnterprise(42)

    setActivePinia(createPinia())
    const refreshed = useWorkspaceStore()
    refreshed.syncIdentity(17, 'account-token-a')

    expect(refreshed.activeSpace).toBe('enterprise')
    expect(refreshed.activeEnterpriseId).toBe(42)
  })

  it('账号或认证令牌变化时清除上一个会话空间和企业选择', () => {
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, 'account-token-a')
    workspace.activateEnterprise(42)

    workspace.syncIdentity(18, 'account-token-b')
    expect(workspace.activeSpace).toBe('personal')
    expect(workspace.activeEnterpriseId).toBeNull()

    workspace.activateEnterprise(53)
    workspace.syncIdentity(18, 'rotated-token-c')
    expect(workspace.activeSpace).toBe('personal')
    expect(workspace.activeEnterpriseId).toBeNull()
  })

  it('切回个人空间会清除企业选择，避免跨空间残留企业上下文', () => {
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, 'account-token-a')
    workspace.activateEnterprise(42)
    workspace.activatePersonal()

    expect(workspace.activeSpace).toBe('personal')
    expect(workspace.activeEnterpriseId).toBeNull()
  })
})
