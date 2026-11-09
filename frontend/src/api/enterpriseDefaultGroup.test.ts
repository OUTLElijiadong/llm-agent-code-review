import { expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }))
vi.mock('./http', () => http)
import { listEnterpriseDirectory, listEnterpriseGroups, listEnterpriseInvitations, listEnterpriseMembers,
  setEnterpriseMemberDefaultGroup } from './enterprise'

it('默认组 API 使用服务端已有路径、字段与认证快照', async () => {
  await setEnterpriseMemberDefaultGroup(10, 2, 11, 'session-a')
  expect(http.put).toHaveBeenCalledWith('/enterprises/10/members/2/default-group',
    { group_id: 11 }, { authTokenSnapshot: 'session-a' })
  await setEnterpriseMemberDefaultGroup(10, 2, null, 'session-a')
  expect(http.put).toHaveBeenLastCalledWith('/enterprises/10/members/2/default-group',
    { group_id: null }, { authTokenSnapshot: 'session-a' })
})

it('组织读取可绑定认证快照并保留原调用方式', async () => {
  await listEnterpriseGroups(10, 'session-a')
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/groups', undefined, undefined, false,
    { authTokenSnapshot: 'session-a' })
  await listEnterpriseMembers(10, 'session-a')
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/members', undefined, undefined, false,
    { authTokenSnapshot: 'session-a' })
  await listEnterpriseInvitations(10, 'session-a')
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/invitations', undefined, undefined, false,
    { authTokenSnapshot: 'session-a' })
  await listEnterpriseDirectory(10, 100, 'session-a')
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/directory', { offset: 100, limit: 100 },
    undefined, false, { authTokenSnapshot: 'session-a' })
  await listEnterpriseGroups(10)
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/groups')
  await listEnterpriseDirectory(10)
  expect(http.get).toHaveBeenLastCalledWith('/enterprises/10/directory', { offset: 0, limit: 100 })
})
