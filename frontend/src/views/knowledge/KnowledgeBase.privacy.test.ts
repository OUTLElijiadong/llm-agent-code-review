import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ docs: vi.fn(), stats: vi.fn(), search: vi.fn(), add: vi.fn(), remove: vi.fn(), sync: vi.fn(), confirm: vi.fn(), close: vi.fn() }))
vi.mock('@/api/knowledge', () => ({ getDocs: api.docs, getKbStats: api.stats, searchKnowledge: api.search, addDoc: api.add, deleteDoc: api.remove, syncKnowledge: api.sync }))
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: api.confirm, close: api.close } }))
import KnowledgeBase from './KnowledgeBase.vue'
import { useUserStore } from '@/stores/user'
const entry = { id: 1, title: '账号 A 私人笔记', source_type: 'manual', char_count: 50, chunk_count: 1 }
beforeEach(() => {
  setActivePinia(createPinia())
  api.docs.mockReset().mockResolvedValue({ items: [], total: 0 })
  api.stats.mockReset().mockResolvedValue({ doc_count: 0, chunk_count: 0 })
  api.search.mockReset().mockResolvedValue([])
  api.confirm.mockReset(); api.remove.mockReset()
})
function render() {
  return mount(KnowledgeBase, { global: { directives: { loading: () => undefined }, stubs: {
    'el-card': { template: '<div><slot /><slot name="header" /></div>' },
    'el-button': { template: '<button><slot /></button>' }, 'el-input': true,
    'el-select': true, 'el-option': true, 'el-pagination': true, 'el-dialog': true,
    'el-form': true, 'el-form-item': true, 'el-tag': { template: '<span><slot /></span>' },
    'el-table': { props: ['data'], template: '<div class="docs"><span v-for="row in data">{{ row.title }}</span><slot /></div>' }, 'el-table-column': true,
  } } })
}
it('个人知识列表与检索的迟到结果不得回写新账号', async () => {
  const user = useUserStore(); user.profile = { id: 101, username: 'a', role: 'user', status: 1 }
  let resolveDocs!: (value: unknown) => void, resolveSearch!: (value: unknown) => void
  api.docs.mockReturnValueOnce(new Promise(done => { resolveDocs = done }))
  api.search.mockReturnValueOnce(new Promise(done => { resolveSearch = done }))
  const wrapper = render()
  const vm = wrapper.vm as unknown as { query: string; doSearch: () => Promise<void>; hits: unknown[] }
  vm.query = '账号 A 私密检索词'; const search = vm.doSearch()
  user.profile = { id: 202, username: 'b', role: 'user', status: 1 }; await flushPromises()
  resolveDocs({ items: [entry], total: 1 }); resolveSearch([{ ...entry, content: '账号 A 私密正文', score: 1 }])
  await search; await flushPromises()
  expect(wrapper.text()).not.toContain('账号 A 私人笔记')
  expect(wrapper.text()).not.toContain('账号 A 私密正文')
  expect(vm.query).toBe('')
  expect(vm.hits).toEqual([])
  wrapper.unmount()
})
it('删除确认弹窗跨账号后不能继续以新账号发起旧文档删除', async () => {
  const user = useUserStore(); user.profile = { id: 101, username: 'a', role: 'user', status: 1 }
  let confirm!: () => void
  api.confirm.mockReturnValueOnce(new Promise<void>(done => { confirm = done }))
  const wrapper = render(); await flushPromises()
  const vm = wrapper.vm as unknown as { remove: (doc: unknown) => Promise<void> }
  const removing = vm.remove(entry)
  user.profile = { id: 202, username: 'b', role: 'user', status: 1 }; await flushPromises()
  confirm(); await removing; await flushPromises()
  expect(api.remove).not.toHaveBeenCalled()
  expect(api.close).toHaveBeenCalledOnce()
  wrapper.unmount()
})
