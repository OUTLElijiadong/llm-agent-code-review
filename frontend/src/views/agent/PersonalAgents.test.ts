import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), disable: vi.fn(),
  run: vi.fn(), getRun: vi.fn(), getRunByRequest: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'session-one' }))
const route = vi.hoisted(() => ({ leave: null as null | (() => boolean) }))
vi.mock('vue-router', async importOriginal => ({
  ...await importOriginal<typeof import('vue-router')>(),
  onBeforeRouteLeave: (guard: () => boolean) => { route.leave = guard },
}))
vi.mock('@/api/personalAgents', () => ({ listPersonalAgents: api.list, getPersonalAgent: api.get,
  createPersonalAgent: api.create, updatePersonalAgent: api.update, disablePersonalAgent: api.disable,
  createPersonalAgentRun: api.run, getPersonalAgentRun: api.getRun, getPersonalAgentRunByRequest: api.getRunByRequest }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token, setToken: vi.fn(), clearToken: vi.fn() }))
vi.mock('./PersonalPublicationPanel.vue', () => ({ default: { template: '<div />' } }))
import { useUserStore } from '@/stores/user'
import PersonalAgents from './PersonalAgents.vue'

const agent = { id: 3, title: '我的助手', prompt: '遵循项目规范', current_version: 1, status: 'active',
  execution_available: false, publication_available: false, create_time: '2026-11-08', update_time: '2026-11-08' }

beforeEach(() => {
  sessionStorage.clear()
  setActivePinia(createPinia())
  const user = useUserStore()
  user.profile = { id: 10, username: 'member', role: 'user', status: 1 }
  user.token = 'session-one'
  auth.token = 'session-one'
  Object.values(api).forEach(fn => fn.mockReset())
  api.list.mockResolvedValue([agent]); api.get.mockResolvedValue(agent)
  vi.stubGlobal('crypto', { randomUUID: () => 'test-key-123',
    subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
})
afterEach(() => { vi.useRealTimers(); Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }) })

describe('个人基础 Agent', () => {
  it('按 Unicode 字符检查长度并拒绝控制字符', async () => {
    api.create.mockResolvedValue(agent)
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-action="new"]').trigger('click')
    await wrapper.get('#personal-agent-title').setValue('助手')
    await wrapper.get('#personal-agent-prompt').setValue('😀'.repeat(30001))
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('指令最多 30000 字符')
    expect(api.create).not.toHaveBeenCalled()
    await wrapper.get('#personal-agent-prompt').setValue('有效\u0000内容')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('不允许的控制字符')
    await wrapper.get('#personal-agent-prompt').setValue('😀'.repeat(30000))
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.create).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('普通账号可创建私有配置，页面准确说明暂不可运行和发布', async () => {
    api.create.mockResolvedValue(agent)
    const wrapper = mount(PersonalAgents)
    await flushPromises()
    expect(wrapper.text()).toContain('个人配置仅自己可见')
    await wrapper.get('[data-action="new"]').trigger('click')
    await wrapper.get('#personal-agent-title').setValue('我的助手')
    await wrapper.get('#personal-agent-prompt').setValue('遵循项目规范')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.create).toHaveBeenCalledWith({ title: '我的助手', prompt: '遵循项目规范', request_key: 'test-key-123' }, 'session-one')
    expect(wrapper.text()).toContain('已保存')
    wrapper.unmount()
  })

  it('保存失败后相同内容重试使用同一幂等键，版本冲突保留编辑', async () => {
    api.create.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(agent)
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-action="new"]').trigger('click')
    await wrapper.get('#personal-agent-title').setValue('我的助手')
    await wrapper.get('#personal-agent-prompt').setValue('遵循项目规范')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.create.mock.calls[0][0].request_key).toBe(api.create.mock.calls[1][0].request_key)
    api.update.mockRejectedValue({ code: 40901 })
    await wrapper.get('#personal-agent-prompt').setValue('保留修改')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('版本已变化')
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe('保留修改')
    wrapper.unmount()
  })

  it('账号切换清空旧内容并忽略迟到响应', async () => {
    let resolveOld!: (value: unknown) => void
    api.list.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve })).mockResolvedValue([])
    const wrapper = mount(PersonalAgents)
    await flushPromises()
    const user = useUserStore(); auth.token = 'session-two'
    user.token = 'session-two'; user.profile = { id: 11, username: 'other', role: 'user', status: 1 }
    await flushPromises(); resolveOld([agent]); await flushPromises()
    expect(wrapper.text()).not.toContain('我的助手')
    wrapper.unmount()
  })

  it('计算请求标识期间切换会话不会用新会话提交旧内容', async () => {
    let resolveDigest!: (value: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key-123',
      subtle: { digest: () => new Promise<ArrayBuffer>(resolve => { resolveDigest = resolve }) } })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-action="new"]').trigger('click')
    await wrapper.get('#personal-agent-title').setValue('旧账号内容')
    await wrapper.get('#personal-agent-prompt').setValue('旧账号指令')
    const submit = wrapper.get('form').trigger('submit')
    await Promise.resolve()
    const user = useUserStore(); auth.token = 'session-two'; user.token = 'session-two'
    user.profile = { id: 11, username: 'other', role: 'user', status: 1 }
    resolveDigest(new Uint8Array([4, 5, 6]).buffer)
    await submit; await flushPromises()
    expect(api.create).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('旧账号内容')
    wrapper.unmount()
  })

  it('停用使用当前版本并保留历史状态', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.disable.mockResolvedValue({ ...agent, status: 'disabled' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-action="disable"]').trigger('click'); await flushPromises()
    expect(api.disable).toHaveBeenCalledWith(3, { expected_version: 1 }, 'session-one')
    expect(wrapper.text()).toContain('已停用')
    wrapper.unmount()
  })

  it('加载更多时从已读取的数量继续查询', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => ({ ...agent, id: index + 1 }))
    api.list.mockResolvedValueOnce(firstPage).mockResolvedValueOnce([{ ...agent, id: 51 }])
    const wrapper = mount(PersonalAgents)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '加载更多')!.trigger('click')
    await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith('session-one', 50)
    expect(wrapper.find('[data-agent="51"]').exists()).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === '加载更多')).toBe(false)
    wrapper.unmount()
  })

  it('未保存的编辑切换或新建前要求确认，取消时保留内容', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    api.list.mockResolvedValue([agent, { ...agent, id: 4, title: '另一个' }])
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-prompt').setValue('尚未保存的指令')
    await wrapper.get('[data-agent="4"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(api.get).not.toHaveBeenCalledWith(4, 'session-one')
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe('尚未保存的指令')
    await wrapper.get('[data-action="new"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe('尚未保存的指令')
    wrapper.unmount()
  })

  it('确认放弃后切换，且停用时明确告知未保存内容会丢失', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.list.mockResolvedValue([agent, { ...agent, id: 4, title: '另一个' }])
    api.get.mockImplementation(async (id: number) => id === 4 ? { ...agent, id, title: '另一个' } : agent)
    api.disable.mockResolvedValue({ ...agent, id: 4, title: '另一个', status: 'disabled' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-prompt').setValue('草稿')
    await wrapper.get('[data-agent="4"]').trigger('click'); await flushPromises()
    expect((wrapper.get('#personal-agent-title').element as HTMLInputElement).value).toBe('另一个')
    await wrapper.get('#personal-agent-prompt').setValue('停用前未保存')
    await wrapper.get('[data-action="disable"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenLastCalledWith(expect.stringContaining('停用会放弃修改'))
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe(agent.prompt)
    wrapper.unmount()
  })

  it('版本冲突后可选择重新加载，取消时不丢弃编辑', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    api.update.mockRejectedValue({ code: 40901 })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-prompt').setValue('待保留的修改')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    await wrapper.get('[data-action="reload-conflict"]').trigger('click'); await flushPromises()
    expect(confirm).toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe('待保留的修改')
    confirm.mockReturnValue(true)
    api.get.mockResolvedValue({ ...agent, current_version: 2, prompt: '服务器的新版本' })
    await wrapper.get('[data-action="reload-conflict"]').trigger('click'); await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).value).toBe('服务器的新版本')
    wrapper.unmount()
  })

  it('未保存时离开路由和刷新提供保护，切换账号不被阻挡', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-action="new"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-title').setValue('尚未保存')
    expect(route.leave?.()).toBe(false)
    expect(confirm).toHaveBeenCalled()
    const refresh = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(refresh)
    expect(refresh.defaultPrevented).toBe(true)
    auth.token = 'session-two'
    expect(route.leave?.()).toBe(true)
    const switchRefresh = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(switchRefresh)
    expect(switchRefresh.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('窄屏切回列表保留未保存输入，并可用键盘回到编辑区', async () => {
    const wrapper = mount(PersonalAgents, { attachTo: document.body })
    await flushPromises(); await wrapper.get('[data-action="new"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-title').setValue('尚未保存')
    expect(document.activeElement?.id).toBe('personal-agent-title')
    await wrapper.get('[data-action="back-list"]').trigger('click'); await flushPromises()
    expect(document.activeElement?.getAttribute('aria-label')).toBe('我的 Agent 列表')
    expect((wrapper.get('#personal-agent-title').element as HTMLInputElement).value).toBe('尚未保存')
    await wrapper.get('[data-action="back-editor"]').trigger('click'); await flushPromises()
    expect(document.activeElement?.id).toBe('personal-agent-title')
    wrapper.unmount()
  })

  it('已停用配置详情打开后聚焦标题', async () => {
    api.get.mockResolvedValue({ ...agent, status: 'disabled' })
    const wrapper = mount(PersonalAgents, { attachTo: document.body })
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    expect(document.activeElement?.tagName).toBe('H3')
    expect(document.activeElement?.textContent).toContain('版本 1')
    wrapper.unmount()
  })

  it('只有后端明确允许时开放运行，按已保存版本显示结果和积分', async () => {
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-action="run"]').exists()).toBe(false)
    wrapper.unmount()
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'succeeded',
      output: '处理完成', actual_milli: 1500, created_at: '2026-11-08T00:00:00Z' })
    const available = mount(PersonalAgents)
    await flushPromises(); await available.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await available.get('#personal-agent-run-input').setValue('需要分析的内容')
    await available.get('[data-action="run"]').trigger('click'); await flushPromises()
    expect(api.run).toHaveBeenCalledWith(3, { request_key: 'test-key-123', version_number: 1,
      input_text: '需要分析的内容' }, 'session-one')
    expect(available.text()).toContain('处理完成')
    expect(available.text()).toContain('1.5 积分')
    expect(available.find('[data-action="run"]').exists()).toBe(false)
    expect(available.text()).toContain('新建运行')
    available.unmount()
  })

  it('无响应时同内容手动重试保持请求键，未知用量不显示零费用', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 31, agent_id: 3,
      version_number: 1, status: 'awaiting_reconciliation', output: null, actual_milli: null, created_at: '2026-11-08' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('保持相同内容')
    await wrapper.get('[data-action="run"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    expect((wrapper.get('#personal-agent-run-input').element as HTMLTextAreaElement).disabled).toBe(false)
    api.getRunByRequest.mockRejectedValueOnce({ code: 40400 })
    await wrapper.findAll('button').find(button => button.text() === '查询状态')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#personal-agent-run-input').element as HTMLTextAreaElement).value).toBe('保持相同内容')
    await wrapper.get('[data-action="retry-run"]').trigger('click'); await flushPromises()
    expect(api.run.mock.calls[1][1].request_key).toBe(api.run.mock.calls[0][1].request_key)
    expect(wrapper.text()).toContain('待核对')
    expect(wrapper.text()).not.toContain('0 积分')
    expect((wrapper.get('#personal-agent-prompt').element as HTMLTextAreaElement).disabled).toBe(true)
    expect((wrapper.get('[data-action="disable"]').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })

  it('额度不足显示充值提示并保留原请求键', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockRejectedValueOnce({ code: 40200 }).mockResolvedValueOnce({ id: 31, agent_id: 3,
      version_number: 1, status: 'succeeded', output: '完成', actual_milli: 100, created_at: '2026-11-08' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('分析输入')
    await wrapper.get('[data-action="run"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('积分或权益不足')
    await wrapper.get('[data-action="retry-run"]').trigger('click'); await flushPromises()
    expect(api.run.mock.calls[1][1].request_key).toBe(api.run.mock.calls[0][1].request_key)
    wrapper.unmount()
  })

  it('刷新页面后仅凭账号范围的请求标识查询，不再次提交模型', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    sessionStorage.setItem('prism:personal-agent-run:10:3', JSON.stringify({ request_key: 'run-key',
      version_number: 1, input_hash: '010203' }))
    api.getRunByRequest.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'running',
      output: null, actual_milli: null, created_at: '2026-11-08' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    expect(api.getRunByRequest).toHaveBeenCalledWith(3, 'run-key', 'session-one')
    expect(api.run).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('运行中')
    expect((wrapper.get('#personal-agent-run-input').element as HTMLTextAreaElement).value).toBe('')
    wrapper.unmount()
  })

  it('刷新后未找到旧请求时可以逐字恢复输入，再使用原请求键重试', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    sessionStorage.setItem('prism:personal-agent-run:10:3', JSON.stringify({ request_key: 'old-run-key',
      version_number: 1, input_hash: '010203' }))
    api.getRunByRequest.mockRejectedValue({ code: 40400 })
    api.run.mockResolvedValue({ id: 9, agent_id: 3, version_number: 1, status: 'running',
      output: null, actual_milli: null })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    const input = wrapper.get('#personal-agent-run-input')
    await input.setValue('原')
    expect((input.element as HTMLTextAreaElement).disabled).toBe(false)
    await input.setValue('原始内容')
    await wrapper.get('[data-action="retry-run"]').trigger('click'); await flushPromises()
    expect(api.run).toHaveBeenCalledWith(3, expect.objectContaining({ request_key: 'old-run-key', input_text: '原始内容' }), 'session-one')
    wrapper.unmount()
  })

  it('其他会话更新配置后仍可用旧版本和旧请求键恢复同一请求', async () => {
    api.get.mockResolvedValue({ ...agent, current_version: 2, execution_available: true })
    sessionStorage.setItem('prism:personal-agent-run:10:3', JSON.stringify({ request_key: 'old-run-key',
      version_number: 1, input_hash: '010203' }))
    api.getRunByRequest.mockRejectedValue({ code: 40400 })
    api.run.mockResolvedValue({ id: 9, agent_id: 3, version_number: 1, status: 'running',
      output: null, actual_milli: null })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('原始内容')
    await wrapper.get('[data-action="retry-run"]').trigger('click'); await flushPromises()
    expect(api.run).toHaveBeenCalledWith(3, expect.objectContaining({ request_key: 'old-run-key', version_number: 1 }), 'session-one')
    wrapper.unmount()
  })

  it('刷新后未找到旧请求，输入不同内容时拒绝用旧请求键提交', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    sessionStorage.setItem('prism:personal-agent-run:10:3', JSON.stringify({ request_key: 'old-run-key',
      version_number: 1, input_hash: 'deadbeef' }))
    api.getRunByRequest.mockRejectedValue({ code: 40400 })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('不是原来的输入')
    await wrapper.get('[data-action="retry-run"]').trigger('click'); await flushPromises()
    expect(api.run).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('内容或版本与原请求不同')
    wrapper.unmount()
  })

  it('已取得运行 ID 的记录优先按 ID 恢复，截断结果不当成完整回答', async () => {
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    sessionStorage.setItem('prism:personal-agent-run:10:3', JSON.stringify({ request_key: 'run-key',
      version_number: 1, input_hash: '010203', run_id: 31 }))
    api.getRun.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'result_unavailable',
      result_reason: 'length', output: null, actual_milli: 100, created_at: '2026-11-08' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    expect(api.getRun).toHaveBeenCalledWith(3, 31, 'session-one')
    expect(api.getRunByRequest).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('本次没有可展示的完整结果')
    expect(wrapper.text()).not.toContain('结果未能保存')
    expect(wrapper.text()).toContain('内容未完整生成')
    expect(wrapper.text()).toContain('0.1 积分')
    expect(wrapper.text()).not.toContain('已完成')
    wrapper.unmount()
  })

  it('运行过程中切换账号不会显示旧账号结果', async () => {
    let finish!: (value: unknown) => void
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('旧账号请求')
    await wrapper.get('[data-action="run"]').trigger('click'); await flushPromises()
    const user = useUserStore(); auth.token = 'session-two'; user.token = 'session-two'
    user.profile = { id: 11, username: 'other', role: 'user', status: 1 }
    finish({ id: 31, agent_id: 3, version_number: 1, status: 'succeeded',
      output: '旧账号结果', actual_milli: 1, created_at: '2026-11-08' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号结果')
    wrapper.unmount()
  })

  it('可见页面运行中低频查询，结束后停止，不能重复提交', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'running',
      output: null, actual_milli: null, created_at: '2026-11-08' })
    api.getRun.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'succeeded',
      output: '已完成', actual_milli: 100, created_at: '2026-11-08' })
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('输入')
    await wrapper.get('[data-action="run"]').trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getRun).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('已完成')
    await vi.advanceTimersByTimeAsync(30000)
    expect(api.getRun).toHaveBeenCalledTimes(1)
    expect(api.run).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('隐藏时不轮询，重新可见时查询且丢弃切换账号后的迟到结果', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    let finish!: (value: unknown) => void
    api.get.mockResolvedValue({ ...agent, execution_available: true })
    api.run.mockResolvedValue({ id: 31, agent_id: 3, version_number: 1, status: 'running',
      output: null, actual_milli: null, created_at: '2026-11-08' })
    api.getRun.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(PersonalAgents)
    await flushPromises(); await wrapper.get('[data-agent="3"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-agent-run-input').setValue('输入')
    await wrapper.get('[data-action="run"]').trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(30000)
    expect(api.getRun).not.toHaveBeenCalled()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(api.getRun).toHaveBeenCalledTimes(1)
    const user = useUserStore(); auth.token = 'session-two'; user.token = 'session-two'
    user.profile = { id: 11, username: 'other', role: 'user', status: 1 }
    finish({ id: 31, agent_id: 3, version_number: 1, status: 'succeeded',
      output: '旧账号内容', actual_milli: 10, created_at: '2026-11-08' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(30000)
    expect(wrapper.text()).not.toContain('旧账号内容')
    expect(api.getRun).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
