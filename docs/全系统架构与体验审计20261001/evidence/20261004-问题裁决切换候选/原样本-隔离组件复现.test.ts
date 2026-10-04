import { shallowMount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import IssueDetailDrawer from './IssueDetailDrawer.vue'
import type { IssueOut } from '@/types/review'

const mocks = vi.hoisted(() => ({ reviewDecision: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('@/api/issue', () => ({ reviewDecision: mocks.reviewDecision }))
vi.mock('element-plus', () => ({ ElMessage: { success: mocks.success, error: mocks.error } }))

const issueA: IssueOut = { id: 11, task_id: 7, issue_type: '安全漏洞', severity: '高',
  title: '待人工核实 A', description: '仅组件样本 A', status: 'pending_review',
  human_review_status: 'pending', create_time: '2026-10-04T00:00:00Z' }
const issueB: IssueOut = { ...issueA, id: 12, title: '待人工核实 B', description: '仅组件样本 B' }
function mountDrawer(issue: IssueOut) {
  return shallowMount(IssueDetailDrawer, {
    props: { modelValue: true, issue },
    global: { stubs: {
      'el-drawer': { props: ['modelValue'], template: '<section v-if="modelValue"><slot /></section>' },
      'el-descriptions': { template: '<dl><slot /></dl>' },
      'el-descriptions-item': { props: ['label'], template: '<div><dt>{{ label }}</dt><dd><slot /></dd></div>' },
      'el-tag': { template: '<span><slot /></span>' },
      'el-button': { template: '<button><slot /></button>' },
      'el-input': { props: ['modelValue'], emits: ['update:modelValue'],
        template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
      SeverityTag: true, AiPromptModal: true,
    } },
  })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((accept) => { resolve = accept })
  return { promise, resolve }
}
afterEach(() => { vi.clearAllMocks() })

describe('IssueDetailDrawer 当前问题裁决绑定 - 隔离复现', () => {
  it.each([1, 2, 3])('第 %s 轮 A 待响应切到 B 后 A 晚返回应不污染 B', async () => {
    const pending = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(pending.promise)
    const wrapper = mountDrawer(issueA)
    await wrapper.get('textarea').setValue('A 的人工依据')
    await wrapper.findAll('button').find((button) => button.text() === '接受结论')!.trigger('click')
    expect(mocks.reviewDecision).toHaveBeenCalledWith(11, { decision: 'accepted', note: 'A 的人工依据' })
    await wrapper.setProps({ issue: issueB })
    await wrapper.get('textarea').setValue('B 的人工依据必须保留')
    expect(wrapper.text()).toContain('仅组件样本 B')
    pending.resolve({ ...issueA, human_review_status: 'accepted', status: 'unfixed' })
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(wrapper.get('textarea').element.value).toBe('B 的人工依据必须保留')
    expect.soft(mocks.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('同一个问题不切换时正常收到结果并清空已提交依据', async () => {
    const pending = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(pending.promise)
    const wrapper = mountDrawer(issueA)
    await wrapper.get('textarea').setValue('A 的人工依据')
    await wrapper.findAll('button').find((button) => button.text() === '接受结论')!.trigger('click')
    const updated = { ...issueA, human_review_status: 'accepted', status: 'unfixed' }
    pending.resolve(updated)
    await flushPromises()
    expect(wrapper.emitted('reviewed')).toEqual([[updated]])
    expect(wrapper.get('textarea').element.value).toBe('')
    expect(mocks.success).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
})
