import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
const api = vi.hoisted(() => ({ issue: vi.fn(), task: vi.fn(), project: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api/aiPrompt', () => ({ listAiPromptTools: vi.fn().mockResolvedValue([]), generatePromptForIssue: api.issue, generatePromptForTask: api.task, generatePromptForProject: api.project }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: api.confirm } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))
import AiPromptModal from './AiPromptModal.vue'
type Source = 'issue' | 'task' | 'project'
const wrappers: ReturnType<typeof shallowMount>[] = []
let user: ReturnType<typeof useUserStore>
beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
  user = useUserStore()
  user.token = 'local-prompt-account'
  user.profile = { id: 11, username: 'local', role: 'user', status: 1 }
  user.permissions = new Set(['issue:view', 'review:view', 'project:view'])
  api.confirm.mockResolvedValue(true)
  for (const call of [api.issue, api.task, api.project]) call.mockResolvedValue({ prompts: [], aggregates: [], summary: '本地模板样本' })
})
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
function render(source: Source, canPolish?: boolean) {
  const slot = { template: '<div><slot /></div>' }
  const wrapper = shallowMount(AiPromptModal, { props: { modelValue: true, source, refId: 7, canPolish } as any,
    global: { stubs: { 'el-dialog': slot, 'el-select': slot, 'el-option': true,
      'el-checkbox': { props: ['disabled'], template: '<label :data-disabled="disabled"><slot /></label>' },
      'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' } } } })
  wrappers.push(wrapper)
  return { wrapper, vm: wrapper.vm as any }
}
const calls = { issue: api.issue, task: api.task, project: api.project }
describe('R3 AI 修复手册执行能力与只读模板', () => {
  it.each(['issue', 'task', 'project'] as const)('%s viewer 默认模板生成不发模型调用也不弹润色确认', async source => {
    const { vm } = render(source, false)
    await vm.generate()
    expect(calls[source]).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ use_llm: false }))
    expect(api.confirm).not.toHaveBeenCalled()
  })
  it.each([false, undefined])('能力未授予时强行勾选润色仍拒绝 %s', async canPolish => {
    const { vm } = render('project', canPolish)
    vm.useLlm = true
    await vm.generate()
    expect(api.project).not.toHaveBeenCalled()
    expect(api.confirm).not.toHaveBeenCalled()
  })
  it.each(['账号', '资源', '能力', '全局权限'] as const)('润色确认期间%s变化不调用旧或新目标', async scenario => {
    let resolve!: () => void
    api.confirm.mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { vm, wrapper } = render('project', true)
    vm.useLlm = true
    const request = vm.generate()
    await flushPromises()
    if (scenario === '账号') user.token = 'other-account'
    else if (scenario === '资源') await wrapper.setProps({ refId: 8 })
    else if (scenario === '能力') await wrapper.setProps({ canPolish: false } as any)
    else user.permissions.delete('project:view')
    resolve(); await request
    expect(api.project).not.toHaveBeenCalled()
  })
  it('合法执行能力保留显式确认后润色', async () => {
    const { vm } = render('project', true)
    vm.useLlm = true
    await vm.generate()
    expect(api.confirm).toHaveBeenCalledTimes(1)
    expect(api.project).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ project_id: 7, use_llm: true }))
  })

  it('润色确认尚未结束时重复生成不增加确认或模型请求', async () => {
    let resolve!: () => void
    api.confirm.mockReturnValueOnce(new Promise<void>(done => { resolve = done }))
    const { vm } = render('project', true)
    const first = vm.generate()
    await vm.generate()
    expect(api.confirm).toHaveBeenCalledTimes(1)
    resolve(); await first
    expect(api.project).toHaveBeenCalledTimes(1)
  })

  it.each(['关闭', '换目标再返回', '卸载'] as const)('模板响应迟到时%s不展示旧内容', async scenario => {
    let resolve!: (value: any) => void
    api.project.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const { vm, wrapper } = render('project', false)
    const request = vm.generate()
    await flushPromises()
    if (scenario === '关闭') await wrapper.setProps({ modelValue: false })
    else if (scenario === '换目标再返回') {
      await wrapper.setProps({ refId: 8 }); await wrapper.setProps({ refId: 7 })
    } else wrapper.unmount()
    resolve({ prompts: [], summary: '旧账号敏感正文' }); await request
    expect(vm.bundle).toBeNull()
  })
})
