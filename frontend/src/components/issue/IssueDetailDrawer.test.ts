import { shallowMount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useUserStore } from '@/stores/user'

const mocks = vi.hoisted(() => ({ reviewDecision: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('@/api/issue', () => ({ reviewDecision: mocks.reviewDecision }))
vi.mock('element-plus', () => ({ ElMessage: { success: mocks.success, error: mocks.error } }))

import IssueDetailDrawer from './IssueDetailDrawer.vue'
import type { IssueOut } from '@/types/review'

const baseIssue: IssueOut = {
  id: 1,
  task_id: 2,
  issue_type: 'security',
  severity: '高',
  title: '命令注入',
  description: '外部输入进入 shell',
  status: 'unfixed',
  can_handle: true,
  create_time: '2026-08-27T00:00:00Z',
}

const wrappers: ReturnType<typeof shallowMount>[] = []

beforeEach(() => {
  setActivePinia(createPinia())
  const user = useUserStore()
  user.token = 'local-component-account-A'
  user.profile = { id: 91, username: 'local-account-A', role: 'reviewer', status: 1 }
  user.permissions = new Set(['issue:handle'])
  vi.clearAllMocks()
  mocks.reviewDecision.mockReset()
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
})

function mountDrawer(issue: IssueOut) {
  const wrapper = shallowMount(IssueDetailDrawer, {
    props: { modelValue: true, issue },
    global: {
      stubs: {
        'el-drawer': {
          name: 'el-drawer',
          props: ['modelValue'],
          emits: ['close'],
          template: '<section v-if="modelValue"><slot /></section>',
        },
        'el-descriptions': { template: '<dl><slot /></dl>' },
        'el-descriptions-item': {
          props: ['label'],
          template: '<div><dt>{{ label }}</dt><dd><slot /></dd></div>',
        },
        'el-tag': { template: '<span><slot /></span>' },
        'el-button': { props: ['loading'], template: '<button :data-loading="loading ? \'yes\' : \'no\'"><slot /></button>' },
        'el-input': {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        SeverityTag: true,
        AiPromptModal: true,
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('IssueDetailDrawer CVSS 可信展示', () => {
  it.each([false, undefined])('R3 只读或未知处理能力隐藏裁决入口且不能提交 %s', async (canHandle) => {
    const wrapper = mountDrawer({ ...baseIssue, can_handle: canHandle, human_review_status: 'pending' })
    expect(wrapper.text()).not.toContain('接受结论')
    await (wrapper.vm as any).submitReview('accepted')
    expect(mocks.reviewDecision).not.toHaveBeenCalled()
  })

  it('score-only 历史数据必须显示未评分且不展示模型分数', () => {
    const wrapper = mountDrawer({
      ...baseIssue,
      cvss_score: 7.5,
      cvss_source: 'model',
    })

    expect(wrapper.text()).toContain('CVSS 评分')
    expect(wrapper.text()).toContain('未评分')
    expect(wrapper.text()).not.toContain('7.5')
  })

  it('有效 v3.1 向量才展示确定性分数和向量', () => {
    const vector = 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'
    const wrapper = mountDrawer({
      ...baseIssue,
      cvss_score: 9.8,
      cvss_vector: vector,
      cvss_version: '3.1',
      cvss_source: 'vector',
    })

    expect(wrapper.text()).toContain('9.8')
    expect(wrapper.text()).toContain(vector)
    expect(wrapper.text()).not.toContain('未评分')
  })
})

describe('IssueDetailDrawer 可信聚合展示', () => {
  it('展示真实来源、冲突和人工复核入口', () => {
    const wrapper = mountDrawer({
      ...baseIssue,
      status: 'pending_review',
      confidence: 0.72,
      confirmation_count: 2,
      aggregation_version: 'finding-aggregation-v1',
      evidence_quality: 'inferred',
      conflict_status: 'unresolved',
      human_review_status: 'pending',
      risk_score: 73.5,
      source_details: [
        { source: 'llm:security', agent_name: '安全审查员', severity: '高', confidence: 0.72 },
        { source: 'llm:reliability', agent_name: '可靠性审查员', severity: '中', confidence: 0.65 },
      ],
    })

    expect(wrapper.text()).toContain('可信聚合')
    expect(wrapper.text()).toContain('真实来源')
    expect(wrapper.text()).toContain('安全审查员')
    expect(wrapper.text()).toContain('待复核')
    expect(wrapper.text()).toContain('接受结论')
    expect(wrapper.text()).toContain('要求补充证据')
  })

  it('历史 verified/not_required 只显示引用匹配，不冒称漏洞已验证', () => {
    const wrapper = mountDrawer({
      ...baseIssue,
      aggregation_version: 'finding-aggregation-v1',
      evidence_quality: 'verified',
      human_review_status: 'not_required',
    })

    expect(wrapper.text()).toContain('源码引用匹配')
    expect(wrapper.text()).toContain('不等同于漏洞条件或影响已验证')
    expect(wrapper.text()).toContain('未要求人工复核（不代表漏洞已验证）')
    expect(wrapper.text()).not.toContain('源码已核验')
  })

  it('未匹配引用和待核实状态分别展示，不被置信度覆盖', () => {
    const wrapper = mountDrawer({
      ...baseIssue,
      aggregation_version: 'finding-aggregation-v1',
      evidence_quality: 'direct',
      human_review_status: 'pending',
      confidence: 0.98,
    })

    expect(wrapper.text()).toContain('引用未匹配')
    expect(wrapper.text()).toContain('待人工核实')
    expect(wrapper.text()).toContain('接受结论')
  })

  it('旧聚合缺复核状态时保持未知，不自动接受', () => {
    const wrapper = mountDrawer({ ...baseIssue, aggregation_version: 'finding-aggregation-v1' })

    expect(wrapper.text()).toContain('未提供复核状态')
    expect(wrapper.text()).not.toContain('人工接受结论')
  })
})


function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}
const pendingA: IssueOut = { ...baseIssue, id: 11, task_id: 7, issue_type: '安全漏洞',
  description: '当前问题 A', status: 'pending_review', human_review_status: 'pending' }
const pendingB: IssueOut = { ...pendingA, id: 12, description: '当前问题 B' }
async function accept(wrapper: ReturnType<typeof mountDrawer>) {
  await wrapper.findAll('button').find((button) => button.text() === '接受结论')!.trigger('click')
}

describe('IssueDetailDrawer 裁决生命周期', () => {
  it.each([1, 2, 3])('第 %s 轮切换 B 后 A 迟到成功不清草稿或发出旧结果', async () => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await wrapper.get('textarea').setValue('A 已提交的依据')
    await accept(wrapper)
    expect(mocks.reviewDecision).toHaveBeenCalledWith(11, { decision: 'accepted', note: 'A 已提交的依据' })
    await wrapper.setProps({ issue: pendingB })
    await wrapper.get('textarea').setValue('B 尚未提交的依据')
    request.resolve({ ...pendingA, human_review_status: 'accepted', status: 'unfixed' })
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(wrapper.get('textarea').element.value).toBe('B 尚未提交的依据')
    expect.soft(mocks.success).not.toHaveBeenCalled()
  })

  it.each(['success', 'error'] as const)('A→B→A 后旧 %s 仍不属于新的 A 生命周期', async (outcome) => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    await wrapper.setProps({ issue: pendingB })
    await wrapper.setProps({ issue: pendingA })
    await wrapper.get('textarea').setValue('重新打开 A 的新依据')
    if (outcome === 'success') request.resolve(pendingA)
    else request.reject(new Error('local-only old failure'))
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(wrapper.get('textarea').element.value).toBe('重新打开 A 的新依据')
    expect.soft(mocks.success).not.toHaveBeenCalled()
    expect.soft(mocks.error).not.toHaveBeenCalled()
  })

  it.each(['success', 'error'] as const)('关闭重开后旧 %s 不污染草稿', async (outcome) => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true })
    await wrapper.get('textarea').setValue('重开后的新依据')
    if (outcome === 'success') request.resolve(pendingA)
    else request.reject(new Error('local-only old failure'))
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(wrapper.get('textarea').element.value).toBe('重开后的新依据')
    expect.soft(mocks.success).not.toHaveBeenCalled()
    expect.soft(mocks.error).not.toHaveBeenCalled()
  })

  it('抽屉关闭事件立即使旧结果失效，不能等待父组件更新属性', async () => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    wrapper.findComponent({ name: 'el-drawer' }).vm.$emit('close')
    await flushPromises()
    request.resolve(pendingA)
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')).toContainEqual([false])
    expect(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect(mocks.success).not.toHaveBeenCalled()
  })

  it.each(['success', 'error'] as const)('卸载后旧 %s 不再产生事件或提示', async (outcome) => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    wrapper.unmount()
    if (outcome === 'success') request.resolve(pendingA)
    else request.reject(new Error('local-only old failure'))
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(mocks.success).not.toHaveBeenCalled()
    expect.soft(mocks.error).not.toHaveBeenCalled()
  })

  it.each(['token', 'profile', 'token-ABA', 'profile-ABA'].flatMap((change) =>
    ['success', 'error'].map((outcome) => ({ change, outcome })),
  ))('$change 改变后旧 $outcome 不污染当前账号显示', async ({ change, outcome }) => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const user = useUserStore()
    const originalProfile = user.profile
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    if (change.startsWith('token')) {
      user.token = 'local-component-account-B'
      if (change === 'token-ABA') user.token = 'local-component-account-A'
    } else {
      user.profile = { id: 92, username: 'local-account-B', role: 'reviewer', status: 1 }
      if (change === 'profile-ABA') user.profile = originalProfile
    }
    await flushPromises()
    await wrapper.get('textarea').setValue('当前账号的新依据')
    if (outcome === 'success') request.resolve(pendingA)
    else request.reject(new Error('local-only old account failure'))
    await flushPromises()
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(wrapper.get('textarea').element.value).toBe('当前账号的新依据')
    expect.soft(mocks.success).not.toHaveBeenCalled()
    expect.soft(mocks.error).not.toHaveBeenCalled()
  })

  it.each(['success', 'error'] as const)('A 迟到 %s 的 finally 不能解除正在提交的 B', async (outcome) => {
    const old = deferred<IssueOut>()
    const current = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const wrapper = mountDrawer(pendingA)
    await accept(wrapper)
    await wrapper.setProps({ issue: pendingB })
    await wrapper.get('textarea').setValue('B 的提交依据')
    await accept(wrapper)
    expect(mocks.reviewDecision).toHaveBeenCalledTimes(2)
    if (outcome === 'success') old.resolve(pendingA)
    else old.reject(new Error('local-only old failure'))
    await flushPromises()
    expect.soft(wrapper.findAll('button')[0].attributes('data-loading')).toBe('yes')
    expect.soft(wrapper.get('textarea').element.value).toBe('B 的提交依据')
    expect.soft(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect.soft(mocks.error).not.toHaveBeenCalled()
    current.resolve({ ...pendingB, human_review_status: 'accepted' })
    await flushPromises()
    expect(wrapper.emitted('reviewed')).toEqual([[{ ...pendingB, human_review_status: 'accepted' }]])
    expect(wrapper.findAll('button')[0].attributes('data-loading')).toBe('no')
  })

  it.each(['token', 'profile'] as const)('缺少 %s 时不提交裁决请求', async (missing) => {
    const user = useUserStore()
    if (missing === 'token') user.token = ''
    else user.profile = null
    const wrapper = mountDrawer(pendingA)
    expect(wrapper.text()).not.toContain('接受结论')
    await (wrapper.vm as any).submitReview('accepted')
    expect(mocks.reviewDecision).not.toHaveBeenCalled()
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('当前成功保持正常提示、结果事件和清空已提交依据', async () => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await wrapper.get('textarea').setValue('本次提交依据')
    await accept(wrapper)
    request.resolve({ ...pendingA, human_review_status: 'accepted' })
    await flushPromises()
    expect(wrapper.emitted('reviewed')).toEqual([[{ ...pendingA, human_review_status: 'accepted' }]])
    expect(wrapper.get('textarea').element.value).toBe('')
    expect(mocks.success).toHaveBeenCalledOnce()
    expect(wrapper.findAll('button')[0].attributes('data-loading')).toBe('no')
  })

  it('当前真实失败仍提示、保留依据并允许重试', async () => {
    const request = deferred<IssueOut>()
    mocks.reviewDecision.mockReturnValueOnce(request.promise)
    const wrapper = mountDrawer(pendingA)
    await wrapper.get('textarea').setValue('失败后应保留的依据')
    await accept(wrapper)
    request.reject(new Error('local-only current failure'))
    await flushPromises()
    expect(wrapper.emitted('reviewed') || []).toHaveLength(0)
    expect(wrapper.get('textarea').element.value).toBe('失败后应保留的依据')
    expect(mocks.error).toHaveBeenCalledWith('复核决定保存失败，问题仍保持待复核，可稍后重试')
    expect(wrapper.findAll('button')[0].attributes('data-loading')).toBe('no')
    mocks.reviewDecision.mockResolvedValueOnce(pendingA)
    await accept(wrapper)
    await flushPromises()
    expect(mocks.reviewDecision).toHaveBeenCalledTimes(2)
  })
})
