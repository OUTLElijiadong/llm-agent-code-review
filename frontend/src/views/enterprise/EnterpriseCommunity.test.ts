import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  listEnterprisePosts: vi.fn(), listEnterpriseAnnouncements: vi.fn(), getEnterprisePost: vi.fn(),
  createEnterprisePost: vi.fn(), updateEnterprisePost: vi.fn(), deleteEnterprisePost: vi.fn(),
  replyEnterprisePost: vi.fn(), deleteEnterpriseReply: vi.fn(), createEnterpriseAnnouncement: vi.fn(),
  updateEnterpriseAnnouncement: vi.fn(), deleteEnterpriseAnnouncement: vi.fn(),
}))
vi.mock('@/api/enterprise', () => mock)
import EnterpriseCommunity from './EnterpriseCommunity.vue'
import { setToken } from '@/utils/token'

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); Object.values(mock).forEach(fn => fn.mockReset()) })
afterEach(() => vi.unstubAllGlobals())

describe('企业论坛和公告', () => {
  const post = (id: number) => ({ id, enterprise_id: 10, author_id: 1, title: `帖子${id}`, content: `正文${id}` })
  const reply = (id: number) => ({ id, enterprise_id: 10, post_id: 55, author_id: 1, content: `回复${id}` })
  const props = { enterpriseId: 10, kind: 'forum' as const, canManage: false, currentUserId: 1, directory: [] }

  it('网络结果未知后重试相同帖子使用同一请求键，成功后可再次发表相同内容', async () => {
    let sequence = 0
    vi.stubGlobal('crypto', { randomUUID: () => `key-${++sequence}`,
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
    mock.listEnterprisePosts.mockResolvedValue({ items: [] })
    mock.createEnterprisePost.mockRejectedValueOnce(new Error('timeout')).mockResolvedValue(post(1))
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises()
    await wrapper.get('#content-title').setValue('标题')
    await wrapper.get('#content-body').setValue('正文')
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    expect(mock.createEnterprisePost.mock.calls[0][3]).toBe('key-1')
    expect(mock.createEnterprisePost.mock.calls[1][3]).toBe('key-1')
    await wrapper.get('#content-title').setValue('标题')
    await wrapper.get('#content-body').setValue('正文')
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    expect(mock.createEnterprisePost.mock.calls[2][3]).toBe('key-2')
    wrapper.unmount()
  })

  it('重试回复沿用请求键且不会储存正文', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'reply-key',
      subtle: { digest: async () => new Uint8Array([7]).buffer } })
    mock.listEnterprisePosts.mockResolvedValue({ items: [post(55)] })
    mock.getEnterprisePost.mockResolvedValue({ post: post(55), replies: [] })
    mock.replyEnterprisePost.mockRejectedValueOnce(new Error('timeout')).mockResolvedValue(reply(1))
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises(); await wrapper.findAll('button').find(item => item.text() === '帖子55')!.trigger('click')
    await flushPromises(); await wrapper.get('#reply-content').setValue('敏感正文')
    await wrapper.get('.detail form').trigger('submit'); await flushPromises()
    expect(JSON.stringify(sessionStorage)).not.toContain('敏感正文')
    await wrapper.get('.detail form').trigger('submit'); await flushPromises()
    expect(mock.replyEnterprisePost.mock.calls[0][3]).toBe('reply-key')
    expect(mock.replyEnterprisePost.mock.calls[1][3]).toBe('reply-key')
    wrapper.unmount()
  })

  it('公告的未知结果可使用原请求键重试', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'notice-key',
      subtle: { digest: async () => new Uint8Array([9]).buffer } })
    mock.listEnterpriseAnnouncements.mockResolvedValue({ items: [] })
    mock.createEnterpriseAnnouncement.mockRejectedValueOnce(new Error('timeout')).mockResolvedValue(post(1))
    const wrapper = mount(EnterpriseCommunity, { props: { ...props, kind: 'announcement', canManage: true } })
    await flushPromises(); await wrapper.get('#content-title').setValue('通知')
    await wrapper.get('#content-body').setValue('内容')
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    expect(mock.createEnterpriseAnnouncement.mock.calls.map(call => call[3])).toEqual(['notice-key', 'notice-key'])
    wrapper.unmount()
  })

  it('计算请求摘要期间切换登录凭据，不得发送旧账号草稿', async () => {
    let resolveDigest!: (value: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => 'key', subtle: { digest: () =>
      new Promise<ArrayBuffer>(resolve => { resolveDigest = resolve }) } })
    setToken('account-a')
    mock.listEnterprisePosts.mockResolvedValue({ items: [] })
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises(); await wrapper.get('#content-title').setValue('甲的标题')
    await wrapper.get('#content-body').setValue('甲的草稿')
    await wrapper.get('.editor').trigger('submit')
    setToken('account-b')
    resolveDigest(new Uint8Array([1]).buffer); await flushPromises()
    expect(mock.createEnterprisePost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('编辑写请求携带操作开始时的登录快照', async () => {
    setToken('account-a')
    mock.listEnterprisePosts.mockResolvedValue({ items: [post(55)] })
    mock.updateEnterprisePost.mockResolvedValue(post(55))
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises(); await wrapper.findAll('button').find(item => item.text() === '编辑')!.trigger('click')
    await wrapper.get('.editor').trigger('submit'); await flushPromises()
    expect(mock.updateEnterprisePost).toHaveBeenCalledWith(10, 55, '帖子55', '正文55', 'account-a')
    wrapper.unmount()
  })

  it('计算回复摘要期间切换凭据，不发送旧账号回复', async () => {
    let resolveDigest!: (value: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => 'key', subtle: { digest: () =>
      new Promise<ArrayBuffer>(resolve => { resolveDigest = resolve }) } })
    setToken('account-a')
    mock.listEnterprisePosts.mockResolvedValue({ items: [post(55)] })
    mock.getEnterprisePost.mockResolvedValue({ post: post(55), replies: [] })
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises(); await wrapper.findAll('button').find(item => item.text() === '帖子55')!.trigger('click')
    await flushPromises(); await wrapper.get('#reply-content').setValue('甲的回复')
    await wrapper.get('.detail form').trigger('submit')
    setToken('account-b')
    resolveDigest(new Uint8Array([1]).buffer); await flushPromises()
    expect(mock.replyEnterprisePost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('可继续加载较旧帖子和较晚回复', async () => {
    mock.listEnterprisePosts.mockResolvedValueOnce({ items: Array.from({ length: 50 }, (_, index) => post(55 - index)) })
      .mockResolvedValueOnce({ items: Array.from({ length: 5 }, (_, index) => post(5 - index)) })
    mock.getEnterprisePost.mockResolvedValueOnce({ post: post(55), replies: Array.from({ length: 100 }, (_, i) => reply(i + 1)) })
      .mockResolvedValueOnce({ post: post(55), replies: Array.from({ length: 5 }, (_, i) => reply(i + 101)) })
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '加载更多帖子')!.trigger('click')
    await flushPromises()
    expect(mock.listEnterprisePosts).toHaveBeenLastCalledWith(10, 6)
    expect(wrapper.text()).toContain('帖子1')
    await wrapper.findAll('button').find(item => item.text() === '帖子55')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '加载更多回复')!.trigger('click')
    await flushPromises()
    expect(mock.getEnterprisePost).toHaveBeenLastCalledWith(10, 55, 100)
    expect(wrapper.text()).toContain('回复105')
    expect(wrapper.findAll('button').some(item => item.text() === '加载更多回复')).toBe(false)
    wrapper.unmount()
  })

  it('切换企业后丢弃上一企业迟到的分页结果', async () => {
    let resolveOlder!: (value: { items: ReturnType<typeof post>[] }) => void
    mock.listEnterprisePosts.mockResolvedValueOnce({ items: Array.from({ length: 50 }, (_, i) => post(55 - i)) })
      .mockImplementationOnce(() => new Promise(resolve => { resolveOlder = resolve }))
      .mockResolvedValueOnce({ items: [] })
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '加载更多帖子')!.trigger('click')
    await wrapper.setProps({ enterpriseId: 20 })
    await flushPromises()
    resolveOlder({ items: [post(1)] })
    await flushPromises()
    expect(wrapper.text()).not.toContain('正文1')
    wrapper.unmount()
  })

  it('公告可继续向前翻页', async () => {
    mock.listEnterpriseAnnouncements.mockResolvedValueOnce({ items: Array.from({ length: 50 }, (_, i) => post(55 - i)) })
      .mockResolvedValueOnce({ items: [post(5)] })
    const wrapper = mount(EnterpriseCommunity, { props: { ...props, kind: 'announcement' } })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '加载更多公告')!.trigger('click')
    await flushPromises()
    expect(mock.listEnterpriseAnnouncements).toHaveBeenLastCalledWith(10, 6)
    expect(wrapper.text()).toContain('正文5')
    wrapper.unmount()
  })

  it('切换帖子后不会将迟到的回复附加到新帖子', async () => {
    let resolveReplies!: (value: { post: ReturnType<typeof post>; replies: ReturnType<typeof reply>[] }) => void
    mock.listEnterprisePosts.mockResolvedValueOnce({ items: [post(55), post(54)] })
    mock.getEnterprisePost.mockResolvedValueOnce({ post: post(55), replies: Array.from({ length: 100 }, (_, i) => reply(i + 1)) })
      .mockImplementationOnce(() => new Promise(resolve => { resolveReplies = resolve }))
      .mockResolvedValueOnce({ post: post(54), replies: [] })
    const wrapper = mount(EnterpriseCommunity, { props })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '帖子55')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '加载更多回复')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '返回帖子列表')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '帖子54')!.trigger('click')
    await flushPromises()
    resolveReplies({ post: post(55), replies: [reply(101)] })
    await flushPromises()
    expect(wrapper.text()).toContain('正文54')
    expect(wrapper.text()).not.toContain('回复101')
    wrapper.unmount()
  })

  it('成员撤权后清除已显示的帖子和草稿', async () => {
    mock.listEnterprisePosts.mockResolvedValueOnce({ items: [
      { id: 1, enterprise_id: 10, author_id: 1, title: '内部标题', content: '内部正文' },
    ] }).mockRejectedValueOnce({ code: 40300 })
    const wrapper = mount(EnterpriseCommunity, { props: {
      enterpriseId: 10, kind: 'forum', canManage: false, currentUserId: 1, directory: [],
    } })
    await flushPromises()
    expect(wrapper.text()).toContain('内部正文')
    await wrapper.get('#content-title').setValue('草稿')
    await wrapper.findAll('button').find(item => item.text() === '刷新')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('内部正文')
    expect(wrapper.text()).toContain('权限')
    expect(wrapper.find('#content-title').exists()).toBe(false)
    wrapper.unmount()
  })
})
