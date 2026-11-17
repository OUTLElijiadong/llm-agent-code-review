import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const enterpriseApi = vi.hoisted(() => ({
  hasEnterpriseMembership: vi.fn<(token: string) => Promise<boolean | null>>(async () => false),
}))

vi.mock('@/services/enterpriseMembership', () => enterpriseApi)

import { useWorkspaceStore } from './workspace'

describe('全站工作空间上下文', () => {
  beforeEach(() => {
    sessionStorage.clear()
    setActivePinia(createPinia())
    enterpriseApi.hasEnterpriseMembership.mockReset().mockResolvedValue(false)
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

  it('公开论坛账号类型从服务端成员目录读取，切换页面空间不会改变授权状态', async () => {
    enterpriseApi.hasEnterpriseMembership.mockResolvedValue(true)
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, 'account-token-a')

    expect(await workspace.refreshEnterpriseMembership()).toBe(true)
    workspace.activatePersonal()

    expect(workspace.hasEnterpriseMembership).toBe(true)
  })

  it('旧账号迟到的企业目录不能覆盖当前登录账号', async () => {
    let resolveOld!: (isMember: boolean | null) => void
    enterpriseApi.hasEnterpriseMembership.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, 'account-token-a')
    const oldRequest = workspace.refreshEnterpriseMembership()

    workspace.syncIdentity(18, 'account-token-b')
    enterpriseApi.hasEnterpriseMembership.mockResolvedValueOnce(false)
    expect(await workspace.refreshEnterpriseMembership()).toBe(false)
    resolveOld(true)

    expect(await oldRequest).toBeNull()
    expect(workspace.hasEnterpriseMembership).toBe(false)
  })

  it('同用户不同 token 即使指纹碰撞也必须重置资格，且不持久化 token', async () => {
    const first = 'synthetic-test-1jc5bd4-52324'
    const second = 'synthetic-test-mtpubf-67639'
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, first)
    workspace.activateEnterprise(42)
    expect(await workspace.refreshEnterpriseMembership()).toBe(false)
    const savedBefore = sessionStorage.getItem('prism.workspace-context.v1')
    workspace.syncIdentity(17, second)
    const savedAfter = sessionStorage.getItem('prism.workspace-context.v1')
    expect(JSON.parse(savedBefore!).identity).toBe(JSON.parse(savedAfter!).identity) // 固定的合成 token 产生同一指纹。
    expect(savedAfter).not.toContain(first)
    expect(savedAfter).not.toContain(second)
    expect(workspace.hasEnterpriseMembership).toBeNull()
    expect(workspace.activeSpace).toBe('personal')
    expect(workspace.activeEnterpriseId).toBeNull()
    enterpriseApi.hasEnterpriseMembership.mockResolvedValueOnce(true)
    expect(await workspace.refreshEnterpriseMembership()).toBe(true)
    expect(enterpriseApi.hasEnterpriseMembership).toHaveBeenLastCalledWith(second)
  })

  it('指纹碰撞后的旧请求及登出后的迟到请求不得写回资格', async () => {
    let resolveOld!: (value: boolean | null) => void
    let resolveNew!: (value: boolean | null) => void
    enterpriseApi.hasEnterpriseMembership.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveNew = resolve }))
    const workspace = useWorkspaceStore()
    workspace.syncIdentity(17, 'synthetic-test-1jc5bd4-52324')
    const oldRequest = workspace.refreshEnterpriseMembership()
    workspace.syncIdentity(17, 'synthetic-test-mtpubf-67639')
    const newRequest = workspace.refreshEnterpriseMembership()
    resolveOld(false)
    expect(await oldRequest).toBeNull()
    expect(workspace.hasEnterpriseMembership).toBeNull()
    const sameNewRequest = workspace.refreshEnterpriseMembership()
    expect(enterpriseApi.hasEnterpriseMembership).toHaveBeenCalledTimes(2)
    workspace.syncIdentity(null, null)
    resolveNew(true)
    expect(await newRequest).toBeNull()
    expect(await sameNewRequest).toBeNull()
    expect(workspace.hasEnterpriseMembership).toBe(false)
  })
})
