import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick, ref, TransitionGroup } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ResponseToolTimeline from './ResponseToolTimeline.vue'
import { applyResponseToolEvent, type ResponseToolCall } from '@/utils/responsesTimeline'

const wrappers: VueWrapper[] = []
const call = (patch: Partial<ResponseToolCall> = {}): ResponseToolCall => ({
  key: 'call-1', name: 'list_projects', argumentsText: '', status: 'running', ...patch,
})

function render(calls: ResponseToolCall[] = []) {
  const wrapper = mount(ResponseToolTimeline, {
    props: { calls }, attachTo: document.body,
    global: { stubs: { Transition: false, TransitionGroup: false, 'el-icon': { template: '<span><slot /></span>' } } },
  })
  wrappers.push(wrapper)
  return wrapper
}

async function expand(wrapper: VueWrapper) {
  await wrapper.get('.xl-steps-toggle').trigger('click')
}

afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

describe('真实步骤与结果动效的交互边界', () => {
  it('真实事件处理器新增响应式调用后展示最终 running，不缓存创建中间态 streaming', async () => {
    const calls = ref<ResponseToolCall[]>([])
    const wrapper = render(calls.value)
    applyResponseToolEvent(calls.value, { type: 'response.tool.started', call_id: 'live-1', tool_name: 'recall_knowledge', arguments: { query: '演示' } })
    await nextTick()
    expect(calls.value[0].status).toBe('running')
    expect(wrapper.get('.xl-step-list').isVisible()).toBe(false)
    expect(wrapper.get('.xl-steps-summary').text()).toContain('1 步进行中')
    await expand(wrapper)
    expect(wrapper.get('.xl-step-note').text()).toBe('正在进行…')
  })

  it('在小菱步骤中显示监督子 Agent 的风险级别和自动审批结论', async () => {
    const calls = ref<ResponseToolCall[]>([])
    const wrapper = render(calls.value)
    applyResponseToolEvent(calls.value, {
      type: 'response.supervisor.reviewed',
      run_id: 'run-1',
      call_id: 'call-1',
      tool_name: 'create_project',
      decision: 'allow',
      risk_level: 'medium',
      classification: 'registered_medium_tool',
      reason: '已登记的可逆或隔离中风险操作',
    })
    applyResponseToolEvent(calls.value, {
      type: 'response.tool.started', call_id: 'call-1', tool_name: 'create_project',
    })
    await nextTick()
    await expand(wrapper)
    expect(wrapper.get('.xl-step-chip.is-supervised').text()).toBe('中风险自动通过')
    expect(wrapper.get('.xl-step-chip.is-supervised').attributes('title')).toContain('隔离中风险')
  })

  it('异常的高风险自动放行事件不会显示为已获准', async () => {
    const calls = ref<ResponseToolCall[]>([])
    const wrapper = render(calls.value)
    applyResponseToolEvent(calls.value, {
      type: 'response.supervisor.reviewed', run_id: 'run-1', call_id: 'call-1', tool_name: 'delete_project',
      decision: 'allow', risk_level: 'high', classification: 'invalid_policy', reason: '高风险不应自动放行',
    })
    await nextTick()
    await expand(wrapper)
    expect(wrapper.get('.xl-step-chip.is-supervised').text()).toBe('监督规则异常')
  })

  it('已上屏的运行步骤失败后折叠摘要即时显示失败项，展开可读完整原因', async () => {
    const wrapper = render([call()])
    await wrapper.setProps({ calls: [call({ status: 'failed', error: '读取失败，请重试' })] })
    expect(wrapper.get('.xl-steps-collapsed-error').text()).toContain('查看项目列表')
    expect(wrapper.get('.xl-step-list').isVisible()).toBe(false)
    await expand(wrapper)
    expect(wrapper.get('.xl-step-error').text()).toBe('读取失败，请重试')
    expect(wrapper.find('.xl-step-spinner').exists()).toBe(false)
  })

  it('等待和取消不计为正在执行或出错', async () => {
    const wrapper = render([
      call({ status: 'waiting_approval' }),
      call({ key: 'call-2', status: 'rejected' }),
    ])
    expect(wrapper.get('.xl-steps-summary').text()).toBe('1 步待确认 · 1 步已取消')
    expect(wrapper.find('.xl-steps-collapsed-errors').exists()).toBe(false)
    await expand(wrapper)
    expect(wrapper.find('.xl-step.is-failed').exists()).toBe(false)
    expect(wrapper.find('.xl-step-spinner').exists()).toBe(false)
  })

  it('Mesh 传输账本摘要不是业务结果，不新增内部追踪元数据展开入口', () => {
    const wrapper = render([call({ status: 'completed', direction: 'receive', resultPreview: '{"trace_id":"private-internal-trace"}' })])
    expect(wrapper.find('.xl-result-toggle').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('private-internal-trace')
  })

  it('折叠摘要可访问且汇总成功、等待、失败，展开面板保留所有完整步骤', async () => {
    const wrapper = render([
      call({ key: 'done', status: 'completed' }),
      call({ key: 'waiting', name: 'ask_user', status: 'waiting_input' }),
      call({ key: 'failed', name: 'run_review', status: 'failed', error: 'finish_reason=length' }),
    ])
    const toggle = wrapper.get<HTMLButtonElement>('.xl-steps-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBeTruthy()
    expect(toggle.attributes('aria-label')).toContain('1 步完成')
    expect(wrapper.get('.xl-steps-summary').text()).toContain('1 步完成')
    expect(wrapper.get('.xl-steps-summary').text()).toContain('1 步待确认')
    expect(wrapper.get('.xl-steps-summary').text()).toContain('1 步出错')
    expect(wrapper.get('.xl-steps-collapsed-error').text()).toContain('run review')
    expect(wrapper.get('.xl-step-error').isVisible()).toBe(false)

    await toggle.trigger('click')
    expect(wrapper.get(`#${toggle.attributes('aria-controls')}`).isVisible()).toBe(true)
    expect(wrapper.findAll('.xl-step')).toHaveLength(3)
    expect(wrapper.get('.xl-step-error').text()).toBe('finish_reason=length')
  })

  it('历史审计阶段只标记记录，当前活动运行才显示进行中', async () => {
    const wrapper = render()
    await wrapper.setProps({ auditPhases: [{ phase: 'scout', label: '侦察员', message: '读取项目' }] })
    expect(wrapper.get('.xl-steps-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.text()).not.toContain('进行中')
    await expand(wrapper)
    await wrapper.setProps({ active: true })
    expect(wrapper.get('.xl-audit-now').text()).toBe('进行中')
    expect(wrapper.get('.xl-steps-summary').text()).toContain('当前：侦察员')
    await wrapper.setProps({ active: false })
    expect(wrapper.text()).not.toContain('进行中')
    expect(wrapper.text()).toContain('侦察员')
  })

  it('成功结果默认折叠、脱敏纯文本展开，焦点留在原生按钮，收起立即删除', async () => {
    const wrapper = render([call({ status: 'completed', resultPreview: '结果 <b>2</b> token=private-token' })])
    expect(wrapper.text()).not.toContain('结果 <b>')
    const toggle = wrapper.get<HTMLButtonElement>('.xl-steps-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    toggle.element.focus()
    await toggle.trigger('click')
    const button = wrapper.get<HTMLButtonElement>('.xl-result-toggle')
    expect(button.attributes('aria-expanded')).toBe('false')
    button.element.focus()
    await button.trigger('click')
    expect(document.activeElement).toBe(button.element)
    expect(button.attributes('aria-expanded')).toBe('true')
    const result = wrapper.get('.xl-step-result')
    expect(result.attributes('id')).toBe(button.attributes('aria-controls'))
    expect(result.text()).toContain('结果 <b>2</b>')
    expect(result.text()).not.toContain('private-token')
    expect(result.find('b').exists()).toBe(false)
    await button.trigger('click')
    expect(wrapper.find('.xl-step-result').exists()).toBe(false)
    expect(document.activeElement).toBe(button.element)
  })

  it('清除数据立即移除展开结果；同一个调用键重新出现不会继承旧展开状态', async () => {
    const wrapper = render([call({ status: 'completed', resultPreview: '私有结果' }), call({ key: 'remaining' })])
    await expand(wrapper)
    await wrapper.get('.xl-result-toggle').trigger('click')
    expect(wrapper.text()).toContain('私有结果')
    await wrapper.setProps({ calls: [call({ key: 'remaining' })] })
    expect(wrapper.text()).not.toContain('私有结果')
    expect(wrapper.findAll('.xl-step')).toHaveLength(1)
    await wrapper.setProps({ calls: [call({ status: 'completed', resultPreview: '新的结果' })] })
    expect(wrapper.get('.xl-steps-toggle').attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.xl-result-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.xl-step-result').exists()).toBe(false)
  })

  it('失败取代结果时立即删除已展开旧内容并显示错误', async () => {
    const wrapper = render([call({ status: 'completed', resultPreview: '旧结果' })])
    await expand(wrapper)
    await wrapper.get('.xl-result-toggle').trigger('click')
    await wrapper.setProps({ calls: [call({ status: 'failed', resultPreview: '旧结果', error: '权限已变更' })] })
    expect(wrapper.find('.xl-step-result').exists()).toBe(false)
    expect(wrapper.get('.xl-step-error').text()).toBe('权限已变更')
  })

  it('减少动态偏好改变时关闭 Vue 过渡，内容仍能展开和立即撤回', async () => {
    let listener: ((event: { matches: boolean }) => void) | undefined
    vi.spyOn(window, 'matchMedia').mockReturnValue({
      matches: false,
      addEventListener: (_event: string, callback: typeof listener) => { listener = callback },
      removeEventListener: vi.fn(),
    } as unknown as MediaQueryList)
    const wrapper = render([call({ status: 'completed', resultPreview: '可读取结果' })])
    await nextTick()
    listener?.({ matches: true })
    await nextTick()
    expect(wrapper.getComponent(TransitionGroup).props('css')).toBe(false)
    await expand(wrapper)
    await wrapper.get('.xl-result-toggle').trigger('click')
    expect(wrapper.get('.xl-step-result').text()).toBe('可读取结果')
    await wrapper.setProps({ calls: [] })
    expect(wrapper.text()).toBe('')
  })
})
