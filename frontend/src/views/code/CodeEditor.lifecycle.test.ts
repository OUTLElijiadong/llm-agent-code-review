import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import { useUserStore } from '@/stores/user'

const api = vi.hoisted(() => ({ getDetail: vi.fn(), update: vi.fn(), downloadBinary: vi.fn() }))
const projects = vi.hoisted(() => ({ getProjectDetail: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('@/api/codeFile', () => api)
vi.mock('@/api/project', () => projects)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))
vi.mock('vue-router', async (importOriginal) => ({ ...await importOriginal<typeof import('vue-router')>(), useRoute: () => route, useRouter: () => ({ back: vi.fn(), push: vi.fn() }) }))
vi.mock('@/components/editor/MonacoEditor.vue', () => ({ default: {
  props: ['modelValue', 'readonly'], emits: ['update:modelValue'],
  template: '<textarea class="draft" :readonly="readonly" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
} }))
import CodeEditor from './CodeEditor.vue'

const route = reactive({ params: { fileId: '1' } })
const baseFile = { id: 1, project_id: 7, file_name: '原文件.py', content: 'OLD_ACCOUNT_CODE', language: 'python', version_no: 1, is_binary: 0, size_bytes: 16, update_time: '2026-10-04T00:00:00Z' }
let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function render() {
  wrapper = mount(CodeEditor, { global: { plugins: [ElementPlus, pinia] } })
  return wrapper
}
beforeEach(() => {
  route.params.fileId = '1'
  pinia = createPinia()
  setActivePinia(pinia)
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 11, username: 'account-a', role: 'user' } as any
  user.permissions = new Set(['project:view', 'file:view', 'file:edit', 'file:download'])
  api.getDetail.mockReset().mockResolvedValue({ ...baseFile })
  api.update.mockReset().mockResolvedValue({ version_no: 2 })
  api.downloadBinary.mockReset().mockResolvedValue(new Blob(['BINARY']))
  projects.getProjectDetail.mockReset().mockResolvedValue({ id: 7, can_update: true })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  URL.createObjectURL = vi.fn(() => 'blob:fixture')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => { wrapper?.unmount() })

describe('CodeEditor 路由和账号异步生命周期', () => {
  it('同组件切换文件时加载新文件，保存使用新文件的版本和内容', async () => {
    render()
    await flushPromises()
    api.getDetail.mockResolvedValueOnce({ ...baseFile, id: 2, file_name: '当前文件.py', content: 'CURRENT_CODE', version_no: 9 })
    route.params.fileId = '2'
    await flushPromises()
    expect(api.getDetail).toHaveBeenLastCalledWith(2)
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('CURRENT_CODE')
    await (wrapper.vm as any).handleSave()
    expect(api.update).toHaveBeenLastCalledWith(2, { content: 'CURRENT_CODE', expected_version: 9 })
  })

  it('加载旧文件期间切换路由，迟到响应不覆盖当前文件', async () => {
    const old = deferred<any>()
    api.getDetail.mockReturnValueOnce(old.promise)
    render()
    await flushPromises()
    api.getDetail.mockResolvedValueOnce({ ...baseFile, id: 2, file_name: '当前文件.py', content: 'CURRENT_CODE' })
    route.params.fileId = '2'
    await flushPromises()
    old.resolve({ ...baseFile })
    await flushPromises()
    expect(wrapper.text()).toContain('当前文件.py')
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('CURRENT_CODE')
  })

  it('已显示内容在读取权限撤回时立即清空，重新授权后可重新加载', async () => {
    render()
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('OLD_ACCOUNT_CODE')
    useUserStore().permissions = new Set()
    await flushPromises()
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect((wrapper.vm as any).codeContent).toBe('')
    useUserStore().permissions = new Set(['file:view'])
    await flushPromises()
    expect(wrapper.get('textarea').attributes('readonly')).toBeDefined()
  })

  it('保存回调在账号改变后不能把旧版本写入新账号内容', async () => {
    render()
    await flushPromises()
    const saved = deferred<any>()
    api.update.mockReturnValueOnce(saved.promise)
    const request = (wrapper.vm as any).handleSave()
    api.getDetail.mockResolvedValueOnce({ ...baseFile, content: 'ACCOUNT_B_CODE', version_no: 7 })
    useUserStore().token = 'account-b-token'
    useUserStore().profile = { id: 12, username: 'account-b', role: 'user' } as any
    await flushPromises()
    saved.resolve({ version_no: 2 })
    await request
    expect((wrapper.vm as any).fileDetail.version_no).toBe(7)
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('ACCOUNT_B_CODE')
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('二进制下载在账号改变后不再触发旧账号文件下载', async () => {
    api.getDetail.mockResolvedValue({ ...baseFile, is_binary: 1 })
    render()
    await flushPromises()
    const downloaded = deferred<Blob>()
    api.downloadBinary.mockReturnValueOnce(downloaded.promise)
    const request = (wrapper.vm as any).handleDownload()
    useUserStore().token = 'another-session'
    await flushPromises()
    downloaded.resolve(new Blob(['OLD_ACCOUNT_BINARY']))
    await request
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled()
  })

  it('组件卸载后完成的详情请求不再发起项目元信息读取', async () => {
    const loaded = deferred<any>()
    api.getDetail.mockReturnValueOnce(loaded.promise)
    render()
    wrapper.unmount()
    loaded.resolve({ ...baseFile })
    await flushPromises()
    expect(projects.getProjectDetail).not.toHaveBeenCalled()
  })

  it('编辑权限撤回保留同账号的当前草稿并立即只读，不重新加载覆盖草稿', async () => {
    render()
    await flushPromises()
    await wrapper.get('textarea').setValue('LOCAL_UNSAVED_DRAFT')
    useUserStore().permissions = new Set(['file:view'])
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('LOCAL_UNSAVED_DRAFT')
    expect(wrapper.get('textarea').attributes('readonly')).toBeDefined()
    expect(api.getDetail).toHaveBeenCalledTimes(1)
    await (wrapper.vm as any).handleSave()
    expect(api.update).not.toHaveBeenCalled()
  })

  it.each(['file:edit', 'project:view'])('撤回再授予%s后，旧项目授权请求不能覆盖新的只读结论', async (permission) => {
    const staleAuthority = deferred<any>()
    projects.getProjectDetail.mockReturnValueOnce(staleAuthority.promise)
    render()
    await flushPromises()
    await wrapper.get('textarea').setValue('DRAFT_DURING_AUTHORITY_REFRESH')
    const permissions = ['project:view', 'file:view', 'file:edit', 'file:download']
    useUserStore().permissions = new Set(permissions.filter((item) => item !== permission))
    await flushPromises()
    projects.getProjectDetail.mockResolvedValueOnce({ id: 7, can_update: false })
    useUserStore().permissions = new Set(permissions)
    await flushPromises()
    expect(wrapper.get('textarea').attributes('readonly')).toBeDefined()
    staleAuthority.resolve({ id: 7, can_update: true })
    await flushPromises()
    expect(wrapper.get('textarea').attributes('readonly')).toBeDefined()
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('DRAFT_DURING_AUTHORITY_REFRESH')
    await (wrapper.vm as any).handleSave()
    expect(api.update).not.toHaveBeenCalled()
  })

  it('文件冲突后仍保留草稿和旧版本，切换文件清除旧冲突提示', async () => {
    render()
    await flushPromises()
    await wrapper.get('textarea').setValue('CONFLICT_DRAFT')
    api.update.mockRejectedValueOnce({ code: 40904 })
    await (wrapper.vm as any).handleSave()
    expect((wrapper.vm as any).codeContent).toBe('CONFLICT_DRAFT')
    expect((wrapper.vm as any).fileDetail.version_no).toBe(1)
    expect(wrapper.text()).toContain('当前编辑草稿仍保留')
    api.getDetail.mockResolvedValue({ ...baseFile, id: 2, content: 'CURRENT_CODE' })
    route.params.fileId = '2'
    await flushPromises()
    expect(wrapper.text()).not.toContain('当前编辑草稿仍保留')
    expect((wrapper.vm as any).codeContent).toBe('CURRENT_CODE')
  })

  it('离开再返回同文件时，旧保存回调不能应用到新加载的相同ID版本', async () => {
    render()
    await flushPromises()
    const saved = deferred<any>()
    api.update.mockReturnValueOnce(saved.promise)
    const request = (wrapper.vm as any).handleSave()
    route.params.fileId = '2'
    await flushPromises()
    api.getDetail.mockResolvedValueOnce({ ...baseFile, version_no: 8 })
    route.params.fileId = '1'
    await flushPromises()
    saved.resolve({ version_no: 2 })
    await request
    expect((wrapper.vm as any).fileDetail.version_no).toBe(8)
    expect(messages.success).not.toHaveBeenCalled()
  })
})
