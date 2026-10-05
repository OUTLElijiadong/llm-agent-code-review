import { defineComponent, reactive } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ForumList from './ForumList.vue'
import ForumPostDetail from './ForumPostDetail.vue'
import ForumPostEdit from './ForumPostEdit.vue'
import type { ForumPostDetail as PostDetail } from '@/api/forum'

const api = vi.hoisted(() => ({
  getPosts: vi.fn(), getPost: vi.fn(), createPost: vi.fn(), updatePost: vi.fn(),
  createReply: vi.fn(), deletePost: vi.fn(), deleteReply: vi.fn(), pinPost: vi.fn(),
  assistDraft: vi.fn(), success: vi.fn(), warning: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api/forum', () => api)
vi.mock('@/stores/user', () => ({ useUserStore: () => user }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: api.success, warning: api.warning } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: api.confirm } }))

const user = reactive({ profile: { id: 1, role: 'user' }, isAdmin: () => user.profile.role === 'admin' })
const wrappers: ReturnType<typeof mount>[] = []
const Slot = { template: '<div><slot /></div>' }
const Button = { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot /></button>' }
const Input = {
  props: ['modelValue', 'type', 'disabled', 'placeholder', 'maxlength'], emits: ['update:modelValue', 'clear'],
  template: '<textarea v-if="type === \'textarea\'" :value="modelValue" :disabled="disabled" :maxlength="maxlength" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" /><input v-else :value="modelValue" :disabled="disabled" :maxlength="maxlength" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" />',
}
const Select = { props: ['modelValue', 'disabled'], emits: ['update:modelValue', 'change'], template: '<select :value="modelValue" :disabled="disabled" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event.target.value)"><slot /></select>' }
const Drawer = defineComponent({ props: ['modelValue', 'size'], template: '<aside v-if="modelValue" data-testid="assist-drawer" :data-size="size"><slot /></aside>' })

function detail(overrides: Partial<PostDetail> = {}): PostDetail {
  return {
    id: 7, user_id: 2, author_name: '发帖者', category: 'qa', title: '原始标题', content: '原始正文',
    view_count: 3, reply_count: 0, is_pinned: false, create_time: '2026-10-06T01:00:00Z',
    update_time: '2026-10-06T01:00:00Z', replies: [], ...overrides,
  }
}
function page(title = '原始标题') { return { items: [detail({ title })], total: 1, page: 1, page_size: 10, pages: 1 } }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function render(component: typeof ForumList | typeof ForumPostDetail | typeof ForumPostEdit, path = '/forum') {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/forum', component: Slot }, { path: '/forum/new', component: Slot },
    { path: '/forum/:id', component: Slot }, { path: '/forum/:id/edit', component: Slot },
  ] })
  await router.push(path); await router.isReady()
  const push = vi.spyOn(router, 'push')
  const wrapper = mount(component, { attachTo: document.body, global: { plugins: [router], directives: { loading: () => {} }, stubs: {
    ElCard: Slot, ElButton: Button, ElInput: Input, ElSelect: Select,
    ElOption: { props: ['label', 'value'], template: '<option :value="value">{{ label }}</option>' },
    ElForm: Slot, ElFormItem: Slot, ElTag: Slot, ElIcon: Slot, ElDrawer: Drawer,
    ElEmpty: { props: ['description'], template: '<p>{{ description }}</p>' },
    ElPagination: { props: ['currentPage', 'total'], emits: ['current-change'], template: '<nav><button @click="$emit(\'current-change\', currentPage + 1)">下一页</button><span>{{ total }}</span></nav>' },
  } } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, push }
}
function button(wrapper: ReturnType<typeof mount>, text: string) {
  const found = wrapper.findAll('button').find(item => item.text() === text || (text === 'AI 发帖助手' && item.text().startsWith(text)))
  expect(found, `应显示“${text}”按钮`).toBeDefined()
  return found!
}

beforeEach(() => {
  vi.clearAllMocks()
  user.profile = { id: 1, role: 'user' }
  api.getPosts.mockReset().mockResolvedValue(page())
  api.getPost.mockReset().mockResolvedValue(detail())
  api.createPost.mockReset().mockResolvedValue({ id: 9 })
  api.updatePost.mockReset().mockResolvedValue(detail())
  api.createReply.mockReset().mockResolvedValue({ id: 8 })
  api.pinPost.mockReset().mockResolvedValue(detail({ is_pinned: true }))
  api.deletePost.mockReset().mockResolvedValue(undefined)
  api.deleteReply.mockReset().mockResolvedValue(undefined)
  api.assistDraft.mockReset().mockResolvedValue({ suggestion: '补充复现步骤', references: [] })
  api.confirm.mockReset().mockResolvedValue(true)
})
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))

describe('论坛列表读取与键盘导航', () => {
  it('旧列表迟到不会覆盖新搜索结果', async () => {
    const old = deferred<ReturnType<typeof page>>()
    api.getPosts.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page('新筛选结果'))
    const { wrapper } = await render(ForumList)
    await wrapper.get('input').setValue('新筛选')
    await button(wrapper, '搜索').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('新筛选结果')
    old.resolve(page('旧请求迟到结果')); await flushPromises()
    expect(wrapper.text()).toContain('新筛选结果')
    expect(wrapper.text()).not.toContain('旧请求迟到结果')
  })

  it('迟到旧错误不会覆盖新请求状态', async () => {
    const old = deferred<ReturnType<typeof page>>()
    api.getPosts.mockReturnValueOnce(old.promise).mockResolvedValueOnce(page('新结果'))
    const { wrapper } = await render(ForumList)
    await button(wrapper, '搜索').trigger('click'); await flushPromises()
    old.reject(new Error('旧请求错误')); await flushPromises()
    expect(wrapper.text()).toContain('新结果')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('新请求仍在读取时，旧响应不能提前撤掉加载状态', async () => {
    const old = deferred<ReturnType<typeof page>>()
    const latest = deferred<ReturnType<typeof page>>()
    api.getPosts.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const { wrapper } = await render(ForumList)
    await button(wrapper, '搜索').trigger('click'); await flushPromises()
    old.resolve(page('不应显示的旧结果')); await flushPromises()
    expect(wrapper.get('.post-list').attributes('aria-busy')).toBe('true')
    expect(wrapper.text()).not.toContain('不应显示的旧结果')
    latest.resolve(page('最终结果')); await flushPromises()
    expect(wrapper.get('.post-list').attributes('aria-busy')).toBe('false')
    expect(wrapper.text()).toContain('最终结果')
  })

  it('读取失败展示错误和重试，不冒充空列表', async () => {
    api.getPosts.mockRejectedValueOnce({ message: '论坛服务暂不可用' })
    const { wrapper } = await render(ForumList)
    expect(wrapper.text()).not.toContain('还没有帖子')
    expect(wrapper.get('[role="alert"]').text()).toContain('论坛服务暂不可用')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('原始标题')
  })

  it('更换筛选后失败不混入上次结果，并按当前筛选重试', async () => {
    const { wrapper } = await render(ForumList)
    api.getPosts.mockRejectedValueOnce(new Error('新筛选读取失败'))
    await wrapper.get('input').setValue('新词')
    await button(wrapper, '搜索').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('原始标题')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(api.getPosts).toHaveBeenLastCalledWith(expect.objectContaining({ keyword: '新词', page: 1 }))
  })

  it('帖子是真实可聚焦链接，原生激活可进入详情', async () => {
    const { wrapper, push } = await render(ForumList)
    const link = wrapper.get('a.post-item')
    expect(link.attributes('href')).toBe('/forum/7')
    ;(link.element as HTMLAnchorElement).focus()
    expect(document.activeElement).toBe(link.element)
    ;(link.element as HTMLAnchorElement).click(); await flushPromises()
    expect(push).toHaveBeenCalledWith('/forum/7')
  })

  it('搜索无结果与首次空社区显示不同提示，清除关键词重新读取', async () => {
    api.getPosts.mockResolvedValue({ ...page(), items: [], total: 0, pages: 0 })
    const { wrapper } = await render(ForumList)
    expect(wrapper.text()).toContain('还没有帖子')
    await wrapper.get('input').setValue('不存在的标题')
    await button(wrapper, '搜索').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('没有找到符合条件的帖子')
    await wrapper.get('input').setValue('')
    wrapper.getComponent(Input).vm.$emit('clear'); await flushPromises()
    expect(api.getPosts).toHaveBeenLastCalledWith(expect.objectContaining({ keyword: '', page: 1 }))
  })

  it('帖子数量缩小时退回存在的页重新读取，不停留在越界空页', async () => {
    const { wrapper } = await render(ForumList)
    await button(wrapper, '下一页').trigger('click'); await flushPromises()
    expect(api.getPosts).toHaveBeenNthCalledWith(2, expect.objectContaining({ page: 2 }))
    expect(api.getPosts).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1 }))
    expect(wrapper.text()).toContain('原始标题')
  })
})

describe('帖子详情权限和失败恢复', () => {
  it.each(['user', 'reviewer', 'admin'])('%s 可回复，管理按钮与现有服务权限一致', async role => {
    user.profile.role = role
    const { wrapper, push } = await render(ForumPostDetail, '/forum/7')
    const labels = wrapper.findAll('button').map(item => item.text())
    expect(labels.includes('发表回复')).toBe(true)
    expect(labels.includes('编辑')).toBe(role === 'admin')
    expect(labels.includes('置顶')).toBe(role === 'admin')
    expect(labels.includes('删除')).toBe(role === 'admin')
    if (role === 'admin') {
      await button(wrapper, '编辑').trigger('click')
      expect(push).toHaveBeenCalledWith('/forum/7/edit')
    }
  })

  it.each(['user', 'reviewer'])('%s 保留本人帖编辑删除入口', async role => {
    user.profile.role = role
    api.getPost.mockResolvedValue(detail({ user_id: 1 }))
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    expect(button(wrapper, '编辑').exists()).toBe(true)
    expect(button(wrapper, '删除').exists()).toBe(true)
    expect(wrapper.findAll('button').some(item => item.text() === '置顶')).toBe(false)
  })

  it.each([
    ['user', 1, true], ['user', 2, false], ['reviewer', 1, true], ['reviewer', 2, false], ['admin', 1, true], ['admin', 2, true],
  ])('%s 对回复作者%s的删除入口为%s', async (role, authorId, allowed) => {
    user.profile.role = role as string
    api.getPost.mockResolvedValue(detail({ replies: [{ id: 8, post_id: 7, user_id: authorId as number, author_name: '回复者', content: '回复内容', create_time: '2026-10-06T01:01:00Z' }] }))
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    expect(wrapper.find('.reply-head button').exists()).toBe(allowed)
  })

  it('详情首次失败可重试恢复，而非只剩返回按钮', async () => {
    api.getPost.mockRejectedValueOnce(new Error('帖子读取失败'))
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    expect(wrapper.get('[role="alert"]').text()).toContain('帖子读取失败')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('原始标题')
  })

  it('回复提交失败保留草稿并显示重试操作', async () => {
    api.createReply.mockRejectedValueOnce({ message: '回复暂未保存' })
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    await wrapper.get('textarea').setValue('不要丢失的回复')
    await button(wrapper, '发表回复').trigger('click'); await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('不要丢失的回复')
    expect(wrapper.get('[role="alert"]').text()).toContain('回复暂未保存')
    await button(wrapper, '重试回复').trigger('click'); await flushPromises()
    expect(api.createReply).toHaveBeenLastCalledWith(7, '不要丢失的回复')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
  })

  it('已成功回复但刷新失败仍确认保存，重试读取不再次提交或计浏览', async () => {
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    api.getPost.mockRejectedValueOnce(new Error('刷新读取失败'))
    await wrapper.get('textarea').setValue('已提交的回复')
    await button(wrapper, '发表回复').trigger('click'); await flushPromises()
    expect(api.success).toHaveBeenCalledWith('回复成功')
    expect(wrapper.get('[role="alert"]').text()).toContain('刷新读取失败')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(api.createReply).toHaveBeenCalledTimes(1)
    expect(api.getPost).toHaveBeenLastCalledWith(7, { recordView: false })
  })

  it('取消删除是正常操作，保持内容且不请求删除', async () => {
    user.profile.role = 'admin'
    api.confirm.mockRejectedValueOnce('cancel')
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    await button(wrapper, '删除').trigger('click'); await flushPromises()
    expect(api.deletePost).not.toHaveBeenCalled()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('原始标题')
  })

  it('管理员置顶等待回执时避免重复请求，成功后刷新状态', async () => {
    user.profile.role = 'admin'
    const pending = deferred<PostDetail>()
    api.pinPost.mockReturnValueOnce(pending.promise)
    const { wrapper } = await render(ForumPostDetail, '/forum/7')
    await button(wrapper, '置顶').trigger('click')
    await button(wrapper, '置顶').trigger('click')
    expect(api.pinPost).toHaveBeenCalledExactlyOnceWith(7, true)
    api.getPost.mockResolvedValue(detail({ is_pinned: true }))
    pending.resolve(detail({ is_pinned: true })); await flushPromises()
    expect(button(wrapper, '取消置顶').exists()).toBe(true)
    expect(api.getPost).toHaveBeenLastCalledWith(7, { recordView: false })
  })
})

describe('发帖与编辑草稿保护', () => {
  beforeEach(() => { user.profile.id = 2 })
  it('读取编辑内容期间禁止输入和提交，成功后使用原内容且不计浏览', async () => {
    const pending = deferred<PostDetail>()
    api.getPost.mockReturnValueOnce(pending.promise)
    const { wrapper } = await render(ForumPostEdit, '/forum/7/edit')
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
    expect(api.getPost).toHaveBeenCalledWith(7, { recordView: false })
    pending.resolve(detail()); await flushPromises()
    expect(wrapper.get('input').attributes('disabled')).toBeUndefined()
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('原始标题')
  })

  it('编辑首次读取失败阻止保存，重试后恢复可编辑表单', async () => {
    api.getPost.mockRejectedValueOnce(new Error('原帖未读取'))
    const { wrapper } = await render(ForumPostEdit, '/forum/7/edit')
    expect(wrapper.get('[role="alert"]').text()).toContain('原帖未读取')
    expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(button(wrapper, '保存').attributes('disabled')).toBeUndefined()
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('原始标题')
  })

  it('非作者编辑他人帖子只读提示，不能从表单提交修改', async () => {
    user.profile.id = 1
    const { wrapper } = await render(ForumPostEdit, '/forum/7/edit')
    expect(wrapper.get('[role="alert"]').text()).toContain('仅帖子作者或管理员')
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
    await button(wrapper, '保存').trigger('click'); await flushPromises()
    expect(api.updatePost).not.toHaveBeenCalled()
  })

  it('保存失败保留标题正文，重试提交当前草稿且不重新拉取覆盖', async () => {
    api.updatePost.mockRejectedValueOnce({ message: '保存暂时失败' })
    const { wrapper, push } = await render(ForumPostEdit, '/forum/7/edit')
    await wrapper.get('input').setValue('新的标题')
    await wrapper.get('textarea').setValue('新的正文')
    await button(wrapper, '保存').trigger('click'); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('保存暂时失败')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('新的标题')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('新的正文')
    await button(wrapper, '重试保存').trigger('click'); await flushPromises()
    expect(api.getPost).toHaveBeenCalledTimes(1)
    expect(api.updatePost).toHaveBeenLastCalledWith(7, { title: '新的标题', category: 'qa', content: '新的正文' })
    expect(push).toHaveBeenCalledWith('/forum/7')
  })

  it.each(['user', 'reviewer', 'admin'])('%s 保留新帖发布流程', async role => {
    user.profile.role = role
    const { wrapper, push } = await render(ForumPostEdit, '/forum/new')
    await wrapper.get('input').setValue('版本更新公告')
    await wrapper.get('textarea').setValue('已核验的更新内容')
    await wrapper.get('select').setValue('announce')
    await button(wrapper, '发布').trigger('click'); await flushPromises()
    expect(api.createPost).toHaveBeenCalledWith({ title: '版本更新公告', content: '已核验的更新内容', category: 'announce' })
    expect(push).toHaveBeenCalledWith('/forum/9')
  })

  it('发布等待回执期间锁定草稿并避免重复提交', async () => {
    const pending = deferred<{ id: number }>()
    api.createPost.mockReturnValueOnce(pending.promise)
    const { wrapper, push } = await render(ForumPostEdit, '/forum/new')
    await wrapper.get('input').setValue('发布标题')
    await wrapper.get('textarea').setValue('发布正文')
    await button(wrapper, '发布').trigger('click')
    await button(wrapper, '发布').trigger('click')
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
    expect(api.createPost).toHaveBeenCalledTimes(1)
    pending.resolve({ id: 9 }); await flushPromises()
    expect(push).toHaveBeenCalledWith('/forum/9')
  })

  it('助手失败清除旧建议并可重试，草稿不丢失', async () => {
    const { wrapper } = await render(ForumPostEdit, '/forum/new')
    await wrapper.get('textarea').setValue('保留的发帖草稿')
    await button(wrapper, 'AI 发帖助手').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('补充复现步骤')
    api.assistDraft.mockRejectedValueOnce(new Error('助手暂不可用'))
    await button(wrapper, 'AI 发帖助手').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('补充复现步骤')
    expect(wrapper.get('[role="alert"]').text()).toContain('助手暂不可用')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('保留的发帖草稿')
    await button(wrapper, '重试助手').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('补充复现步骤')
  })
})
