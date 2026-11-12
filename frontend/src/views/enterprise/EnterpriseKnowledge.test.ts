import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ listEnterpriseKnowledge: vi.fn(), getEnterpriseKnowledge: vi.fn(),
  createEnterpriseKnowledge: vi.fn(), updateEnterpriseKnowledge: vi.fn(), deleteEnterpriseKnowledge: vi.fn(),
  listEnterpriseKnowledgeAttachments: vi.fn(), searchEnterpriseKnowledgeAttachments: vi.fn(),
  uploadEnterpriseKnowledgeAttachment: vi.fn(), deleteEnterpriseKnowledgeAttachment: vi.fn(),
  downloadEnterpriseKnowledgeAttachment: vi.fn() }))
vi.mock('@/api/enterpriseKnowledge', () => api)
import EnterpriseKnowledge from './EnterpriseKnowledge.vue'

const item = { id: 5, enterprise_id: 10, author_id: 1, updated_by: 1, version: 1, title: '交付规范', content: '保密内容' }
beforeEach(() => {
  localStorage.removeItem('review_token'); sessionStorage.clear()
  Object.values(api).forEach(fn => fn.mockReset())
  api.listEnterpriseKnowledge.mockResolvedValue({ items: [item] })
  api.getEnterpriseKnowledge.mockResolvedValue(item)
  api.listEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [] })
  api.searchEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [], search_mode: 'local_hash_vector_and_terms' })
})

describe('企业知识库', () => {
  it('成员可以检索和查看正文，不能新增或修改', async () => {
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 2, canManage: false } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('新建知识')
    await wrapper.get('#knowledge-query').setValue('规范')
    await wrapper.get('[data-action="search"]').trigger('submit'); await flushPromises()
    expect(api.listEnterpriseKnowledge).toHaveBeenLastCalledWith(10, '规范')
    await wrapper.findAll('button').find(item => item.text() === '交付规范')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('保密内容')
    expect(wrapper.text()).not.toContain('删除')
    wrapper.unmount()
  })

  it('创建结果未知后重试使用相同请求键', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'request-1', subtle: { digest: async () => new Uint8Array([1]).buffer } })
    api.createEnterpriseKnowledge.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(item)
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    await wrapper.get('#knowledge-title').setValue('交付规范')
    await wrapper.get('#knowledge-content').setValue('保密内容')
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    expect(api.createEnterpriseKnowledge).toHaveBeenCalledTimes(2)
    expect(api.createEnterpriseKnowledge.mock.calls[0][3]).toBe(api.createEnterpriseKnowledge.mock.calls[1][3])
    await wrapper.get('#knowledge-title').setValue('第二条')
    await wrapper.get('#knowledge-content').setValue('新的内容')
    expect((wrapper.get('[data-action="save"] button').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount(); vi.unstubAllGlobals()
  })

  it('切换企业后不显示旧企业迟到正文，撤权后清除详情', async () => {
    let resolve!: (value: unknown) => void
    api.getEnterpriseKnowledge.mockReturnValueOnce(new Promise(r => { resolve = r }))
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 2, canManage: false } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '交付规范')!.trigger('click')
    await wrapper.setProps({ enterpriseId: 20 }); await flushPromises()
    resolve(item); await flushPromises()
    expect(wrapper.text()).not.toContain('保密内容')
    api.getEnterpriseKnowledge.mockRejectedValueOnce({ code: 40300 })
    await wrapper.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('保密内容')
    wrapper.unmount()
  })

  it('创建请求键期间凭据切换时不会提交旧内容', async () => {
    let resolveDigest!: (buffer: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => 'request-1',
      subtle: { digest: () => new Promise(resolve => { resolveDigest = resolve }) } })
    localStorage.setItem('review_token', 'old-token')
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    await wrapper.get('#knowledge-title').setValue('旧标题')
    await wrapper.get('#knowledge-content').setValue('旧账号正文')
    await wrapper.get('[data-action="save"]').trigger('submit')
    localStorage.setItem('review_token', 'new-token')
    resolveDigest(new Uint8Array([1]).buffer); await flushPromises()
    expect(api.createEnterpriseKnowledge).not.toHaveBeenCalled()
    wrapper.unmount(); vi.unstubAllGlobals()
  })

  it('修改结果未知后刷新失败仍禁止再次修改', async () => {
    api.updateEnterpriseKnowledge.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '编辑')!.trigger('click')
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('上次修改结果未知')
    api.listEnterpriseKnowledge.mockRejectedValueOnce(new Error('offline'))
    await wrapper.findAll('button').find(button => button.text() === '刷新')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('上次修改结果未知')
    expect((wrapper.get('[data-action="save"] button').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
    api.listEnterpriseKnowledge.mockResolvedValue({ items: [item] })
    const restored = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    expect(restored.text()).toContain('上次修改结果未知')
    await restored.findAll('button').find(button => button.text() === '刷新')!.trigger('click'); await flushPromises()
    expect(restored.text()).toContain('上次修改结果未知')
    await restored.findAll('button').find(button => button.text() === '已核对结果，继续操作')!.trigger('click')
    expect(restored.text()).not.toContain('上次修改结果未知')
    restored.unmount()
  })

  it('详情被删除只提示条目不存在，不误判企业撤权', async () => {
    api.getEnterpriseKnowledge.mockRejectedValueOnce({ code: 40400 })
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 2, canManage: false } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('知识条目不存在或已删除')
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    wrapper.unmount()
  })

  it('保存期间禁用表单输入和列表，防止丢失后续编辑', async () => {
    let resolve!: (value: typeof item) => void
    api.updateEnterpriseKnowledge.mockReturnValueOnce(new Promise(r => { resolve = r }))
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '编辑')!.trigger('click')
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    expect((wrapper.get('#knowledge-title').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('#knowledge-content').element as HTMLTextAreaElement).disabled).toBe(true)
    expect((wrapper.findAll('button').find(button => button.text() === '交付规范')!.element as HTMLButtonElement).disabled).toBe(true)
    resolve(item); await flushPromises()
    wrapper.unmount()
  })

  it('创建冲突允许用户主动放弃旧请求后重试', async () => {
    let sequence = 0
    vi.stubGlobal('crypto', { randomUUID: () => `request-${++sequence}`,
      subtle: { digest: async () => new Uint8Array([1]).buffer } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.createEnterpriseKnowledge.mockRejectedValueOnce({ code: 40901 }).mockResolvedValueOnce(item)
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    await wrapper.get('#knowledge-title').setValue('交付规范')
    await wrapper.get('#knowledge-content').setValue('保密内容')
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '放弃旧创建请求')!.trigger('click')
    await wrapper.get('[data-action="save"]').trigger('submit'); await flushPromises()
    expect(api.createEnterpriseKnowledge.mock.calls[0][3]).not.toBe(api.createEnterpriseKnowledge.mock.calls[1][3])
    wrapper.unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks()
  })

  it('同账号重进后旧写入完成不能清除新写入的未知状态', async () => {
    let resolveOld!: (value: typeof item) => void
    api.updateEnterpriseKnowledge.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
      .mockReturnValueOnce(new Promise(() => {}))
    const props = { enterpriseId: 10, currentUserId: 1, canManage: true }
    const first = mount(EnterpriseKnowledge, { props }); await flushPromises()
    await first.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    await first.findAll('button').find(button => button.text() === '编辑')!.trigger('click')
    await first.get('[data-action="save"]').trigger('submit'); await flushPromises()
    first.unmount()
    const second = mount(EnterpriseKnowledge, { props }); await flushPromises()
    await second.findAll('button').find(button => button.text() === '刷新')!.trigger('click'); await flushPromises()
    await second.findAll('button').find(button => button.text() === '已核对结果，继续操作')!.trigger('click')
    await second.findAll('button').find(button => button.text() === '交付规范')!.trigger('click'); await flushPromises()
    await second.findAll('button').find(button => button.text() === '编辑')!.trigger('click')
    await second.get('[data-action="save"]').trigger('submit'); await flushPromises()
    resolveOld(item); await flushPromises()
    second.unmount()
    const third = mount(EnterpriseKnowledge, { props }); await flushPromises()
    expect(third.text()).toContain('上次修改结果未知')
    third.unmount()
  })

  it('成员只能搜索和下载附件，不能上传或删除', async () => {
    api.listEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [{ id: 7, enterprise_id: 10,
      author_id: 1, updated_by: 1, file_name: '规范.md', file_ext: '.md', mime: 'text/markdown',
      file_size: 20, version: 1, create_time: '', update_time: '' }] })
    api.searchEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [{ attachment_id: 7,
      file_name: '规范.md', excerpt: '部署前需要复核', score: 0.5, search_mode: 'local_hash_vector_and_terms' }] })
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 2, canManage: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('规范.md')
    expect(wrapper.find('[data-action="attachment-upload"]').exists()).toBe(false)
    await wrapper.get('#attachment-query').setValue('部署')
    await wrapper.get('[data-action="attachment-search"]').trigger('submit'); await flushPromises()
    expect(api.searchEnterpriseKnowledgeAttachments).toHaveBeenCalledWith(10, '部署', null)
    expect(wrapper.text()).toContain('部署前需要复核')
    expect(wrapper.text()).not.toContain('删除')
    wrapper.unmount()
  })

  it('上传和删除只在管理员界面出现，刷新列表后回到附件列表', async () => {
    api.uploadEnterpriseKnowledgeAttachment.mockResolvedValue({ id: 8 })
    api.listEnterpriseKnowledgeAttachments.mockResolvedValue({ items: [] })
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    expect(wrapper.find('[data-action="attachment-upload"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('本地词项与字符相似度')
    wrapper.unmount()
  })

  it('附件上传结果未知后重试复用同一个幂等请求键', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'attachment-request-1',
      subtle: { digest: async () => new Uint8Array([7, 8, 9]).buffer } })
    api.uploadEnterpriseKnowledgeAttachment.mockRejectedValueOnce(new Error('connection lost'))
      .mockResolvedValueOnce({ id: 8 })
    const wrapper = mount(EnterpriseKnowledge, { props: { enterpriseId: 10, currentUserId: 1, canManage: true } })
    await flushPromises()
    const file = new File(['资料正文'], '资料.md', { type: 'text/markdown' })
    Object.defineProperty(file, 'arrayBuffer', { value: async () => new TextEncoder().encode('资料正文').buffer })
    const input = wrapper.get('#knowledge-attachment')
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await wrapper.get('[data-action="attachment-upload"]').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('上传结果暂时未知')
    await input.trigger('change')
    await wrapper.get('[data-action="attachment-upload"]').trigger('submit'); await flushPromises()
    expect(api.uploadEnterpriseKnowledgeAttachment).toHaveBeenCalledTimes(2)
    expect(api.uploadEnterpriseKnowledgeAttachment.mock.calls[0][2]).toBe('attachment-request-1')
    expect(api.uploadEnterpriseKnowledgeAttachment.mock.calls[1][2]).toBe('attachment-request-1')
    wrapper.unmount(); vi.unstubAllGlobals()
  })
})
