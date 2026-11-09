import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listMyEnterprises: vi.fn(), listEnterpriseGroups: vi.fn(), listEnterpriseDirectory: vi.fn(),
  listEnterpriseConversations: vi.fn(), listEnterpriseKnowledge: vi.fn(), getEnterpriseKnowledge: vi.fn(),
  listEnterpriseKnowledgeAttachments: vi.fn(), searchEnterpriseKnowledgeAttachments: vi.fn(),
}))
const account = vi.hoisted(() => ({ profile: { id: 1 }, token: 'old-token' }))
const workspaceState = vi.hoisted(() => ({
  activeSpace: 'enterprise',
  activeEnterpriseId: null as number | null,
  activateEnterprise: vi.fn((id?: number | null) => { workspaceState.activeEnterpriseId = id ?? null }),
}))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => workspaceState }))
vi.mock('@/api/enterprise', () => ({ ...mocks,
  acceptEnterpriseInvitation: vi.fn(), openEnterpriseConversation: vi.fn(), getEnterpriseMessages: vi.fn(),
  sendEnterpriseMessage: vi.fn(),
}))
vi.mock('@/api/enterpriseKnowledge', () => ({ ...mocks,
  createEnterpriseKnowledge: vi.fn(), updateEnterpriseKnowledge: vi.fn(), deleteEnterpriseKnowledge: vi.fn(),
  listEnterpriseKnowledgeAttachments: mocks.listEnterpriseKnowledgeAttachments,
  searchEnterpriseKnowledgeAttachments: mocks.searchEnterpriseKnowledgeAttachments,
  uploadEnterpriseKnowledgeAttachment: vi.fn(), deleteEnterpriseKnowledgeAttachment: vi.fn(),
  downloadEnterpriseKnowledgeAttachment: vi.fn(),
}))
import EnterpriseCommunication from './EnterpriseCommunication.vue'
import { useUserStore } from '@/stores/user'

beforeEach(() => {
  Object.values(mocks).forEach(fn => fn.mockReset())
  sessionStorage.clear(); localStorage.setItem('review_token', 'old-token')
  useUserStore().token = 'old-token'
  mocks.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'member', status: 'active' }])
  mocks.listEnterpriseGroups.mockResolvedValue([])
  mocks.listEnterpriseDirectory.mockResolvedValue([])
  mocks.listEnterpriseConversations.mockResolvedValue({ items: [], assistant_available: false })
  mocks.listEnterpriseKnowledge.mockResolvedValue({ items: [{ id: 5, title: '旧条目' }] })
  mocks.listEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [] })
  mocks.searchEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [], search_mode: 'local_hash_vector_and_terms' })
})

describe('企业知识登录状态', () => {
  it('同账号令牌切换时旧知识正文迟到不会显示在新会话', async () => {
    let resolveOld!: (value: unknown) => void
    mocks.getEnterpriseKnowledge.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(EnterpriseCommunication); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '企业知识库')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '旧条目')!.trigger('click')
    localStorage.setItem('review_token', 'new-token')
    useUserStore().token = 'new-token'
    await flushPromises()
    resolveOld({ id: 5, title: '旧条目', content: '旧会话不应显示的秘密' }); await flushPromises()
    expect(wrapper.text()).not.toContain('旧会话不应显示的秘密')
    wrapper.unmount()
  })
})
