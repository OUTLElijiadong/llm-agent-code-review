import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { useUserStore } from '@/stores/user'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SecurityScanOut } from '@/types/security'

const api = vi.hoisted(() => ({
  scanFile: vi.fn(), scanTask: vi.fn(), scanProject: vi.fn(), scanAllProjects: vi.fn(),
}))
const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn() }))

vi.mock('@/api/security', () => api)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import SecurityScanModal from './SecurityScanModal.vue'

function scanResult(overrides: Partial<SecurityScanOut> = {}): SecurityScanOut {
  return {
    findings: [], threat_model: null, discussion: null, compliance: {},
    risk_score: 92, summary: '隔离测试返回的扫描摘要', file_count: 3, duration_ms: 1200,
    ...overrides,
  }
}

function deferredResult() {
  let resolve!: (value: SecurityScanOut) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<SecurityScanOut>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>

async function renderModal(props: Record<string, unknown> = {}, realTransition = false) {
  wrapper = mount(SecurityScanModal, {
    props: { modelValue: true, source: 'project', refId: 7, refName: '测试项目', ...props },
    attachTo: document.body,
    global: { plugins: [ElementPlus, pinia], stubs: { transition: !realTransition } },
  })
  await flushPromises()
  return wrapper
}

function button(label: string) {
  const match = wrapper.findAll('button').find((item) => item.text() === label)
  if (!match) throw new Error(`未找到按钮：${label}`)
  return match
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useUserStore().permissions = new Set(['security:scan'])
  for (const request of Object.values(api)) request.mockResolvedValue(scanResult())
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
})

describe('同步安全扫描真实交互', () => {
  it.each([
    ['file', 'scanFile', { file_id: 7, scan_depth: 'standard' }],
    ['task', 'scanTask', { task_id: 7 }],
    ['project', 'scanProject', { project_id: 7, scan_mode: 'static_full', top_n: 50, trace_dataflow: true }],
    ['all-projects', 'scanAllProjects', { scan_mode: 'static_full', top_n_per_project: 50, trace_dataflow: true }],
  ] as const)('首次可见且自动启动时，%s 只使用原有请求契约', async (source, method, payload) => {
    await renderModal({ source, autoStart: true, refId: source === 'all-projects' ? null : 7 })
    expect(api[method]).toHaveBeenCalledExactlyOnceWith(payload)
    expect(wrapper.emitted('completed')).toHaveLength(1)
    expect(wrapper.get('[role="status"]').text()).toContain('已收到扫描结果')
    expect(wrapper.text()).not.toContain('结果已保存')
    expect(messages.success).not.toHaveBeenCalledWith('未检出安全风险')
  })

  it('等待状态不显示假阶段/百分比，配置锁定且重复操作不重复发请求', async () => {
    const pending = deferredResult()
    api.scanProject.mockReturnValue(pending.promise)
    await renderModal()
    await button('开始扫描').trigger('click')
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledOnce()
    expect(wrapper.get('[role="status"]').text()).toContain('等待服务端响应')
    expect(wrapper.text()).toContain('不返回阶段进度')
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/\d+\s*%/)
    expect(wrapper.findAll('input').every((input) => (input.element as HTMLInputElement).disabled)).toBe(true)
    await button('等待服务端响应').trigger('click')
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true, autoStart: true })
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledOnce()
    pending.resolve(scanResult())
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('已收到扫描结果')
    expect(wrapper.text()).toContain('92')
  })

  it('失败展示后端原因和排查编号，明确重试不等于取消前次请求', async () => {
    api.scanProject.mockRejectedValueOnce({ message: '扫描服务暂不可用', request_id: 'test-request', next_action: '请稍后重试' })
    await renderModal({ autoStart: true })
    expect(wrapper.get('[role="alert"]').text()).toContain('扫描服务暂不可用')
    expect(wrapper.get('[role="alert"]').text()).toContain('test-request')
    expect(wrapper.get('[role="alert"]').text()).toContain('请稍后重试')
    expect(wrapper.text()).toContain('不代表服务端已取消')
    expect(wrapper.emitted('completed')).toBeUndefined()
    await button('重试扫描').trigger('click')
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.emitted('completed')).toHaveLength(1)
  })

  it('后端明确禁止重试时，不提供可执行的重复请求', async () => {
    api.scanProject.mockRejectedValueOnce({ message: '没有扫描权限', retryable: false, next_action: '联系管理员' })
    await renderModal({ autoStart: true })
    expect(button('重试扫描').attributes('disabled')).toBeDefined()
    await button('重试扫描').trigger('click')
    expect(api.scanProject).toHaveBeenCalledOnce()
  })

  it('关闭只关闭窗口，重新打开继续等待同一个请求', async () => {
    const pending = deferredResult()
    api.scanProject.mockReturnValue(pending.promise)
    await renderModal({ autoStart: true })
    expect(wrapper.text()).toContain('关闭窗口不会取消服务端扫描')
    await button('关闭窗口（不取消扫描）').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledOnce()
    pending.resolve(scanResult())
    await flushPromises()
  })

  it('真实对话框过渡及焦点建立后，Escape 关闭窗口但不取消扫描', async () => {
    const pending = deferredResult()
    api.scanProject.mockReturnValue(pending.promise)
    await renderModal({ autoStart: true }, true)
    const dialog = wrapper.findComponent({ name: 'ElDialog' })
    await vi.waitFor(() => expect(dialog.emitted('opened')).toHaveLength(1))
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }))
    await vi.waitFor(() => expect(wrapper.emitted('update:modelValue')).toEqual([[false]]))
    expect(api.scanProject).toHaveBeenCalledOnce()
    pending.resolve(scanResult())
    await flushPromises()
  })

  it('范围改变后迟到的结果不能成为新范围的结果', async () => {
    const pending = deferredResult()
    api.scanProject.mockReturnValue(pending.promise)
    await renderModal()
    await button('开始扫描').trigger('click')
    await wrapper.setProps({ refId: 9, refName: '另一个项目' })
    pending.resolve(scanResult({ summary: '旧项目不应展示的摘要' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧项目不应展示的摘要')
    expect(wrapper.emitted('completed')).toBeUndefined()
    expect(wrapper.text()).toContain('另一个项目')
  })

  it('卸载后不发布迟到的完成事件', async () => {
    const pending = deferredResult()
    api.scanFile.mockReturnValue(pending.promise)
    await renderModal({ source: 'file', autoStart: true })
    wrapper.unmount()
    pending.resolve(scanResult())
    await flushPromises()
    expect(wrapper.emitted('completed')).toBeUndefined()
  })

  it('首次打开已保存结果不再次扫描，并标明结果来源', async () => {
    await renderModal({ initialResult: scanResult(), autoStart: true })
    expect(api.scanProject).not.toHaveBeenCalled()
    expect(wrapper.get('[role="status"]').text()).toContain('已加载保存结果')
    expect(button('下载报告').exists()).toBe(true)
  })

  it('任务复审说明真实的标签整理行为，不声称进行 LLM 深度扫描', async () => {
    await renderModal({ source: 'task' })
    expect(wrapper.text()).toContain('不调用模型')
    expect(wrapper.text()).not.toContain('LLM 深度漏洞审查')
  })

  it('缺少目标时给出页面内错误且不调用扫描接口', async () => {
    await renderModal({ refId: null, autoStart: true })
    expect(api.scanProject).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('缺少有效的扫描目标')
  })

  it('下载报告使用已返回结果，不触发新扫描', async () => {
    await renderModal({ autoStart: true })
    const createObjectURL = vi.fn(() => 'blob:isolated-test')
    const revokeObjectURL = vi.fn()
    const NativeURL = URL
    vi.stubGlobal('URL', class extends NativeURL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
    let downloadName = ''
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloadName = this.download
    })
    await button('下载报告').trigger('click')
    expect(downloadName).toBe('prism_security_project_7.md')
    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:isolated-test')
    expect(api.scanProject).toHaveBeenCalledOnce()
  })

  it('证伪候选不混入有效发现统计，并在详情与下载报告保留复核理由', async () => {
    const refutedFinding = Object.assign({
      title: '已证伪候选', category: '认证', owasp: '', cwe: '', severity: '严重',
      file_path: 'auth.py', file_id: null, lines: 'L30', line_number: 30, end_line: 30,
      evidence: 'hash_equals($stored, $input)', exploit_scenario: '', fix_suggestion: '',
      references: [], confidence: 0.3, source: 'llm',
    }, { verification: 'refuted', verification_reason: '比较的是哈希值，证据无法支持明文密码比较。' }) as SecurityScanOut['findings'][number]
    const findings: SecurityScanOut['findings'] = [
      Object.assign({ ...refutedFinding, title: '已确认候选', severity: '高', confidence: 0.95 }, {
        verification: 'confirmed', verification_reason: '可由当前证据确认。',
      }),
      Object.assign({ ...refutedFinding, title: '待复核候选', severity: '高', confidence: 0.6 }, {
        verification: 'unreviewed', verification_reason: '',
      }),
      Object.assign({ ...refutedFinding, title: '旧版未标记候选', severity: '中', confidence: 0.8 }, {
        verification: undefined, verification_reason: '',
      }),
      refutedFinding,
    ]
    api.scanProject.mockResolvedValueOnce(scanResult({
      findings,
      summary: '有效/待复核候选 3 条；已证伪 1 条。',
      compliance: { verification: { confirmed: 1, refuted: 1, pending: 1, unknown: 1, total: 4 } },
    }))
    await renderModal({ autoStart: true })

    expect(wrapper.findAll('.finding-row')).toHaveLength(3)
    expect(wrapper.find('.sev-严重 .cell-num').text()).toBe('0')
    expect(wrapper.find('.sev-高 .cell-num').text()).toBe('2')
    expect(wrapper.find('.sev-中 .cell-num').text()).toBe('1')
    expect(wrapper.find('.refuted-findings summary').text()).toContain('已证伪候选 1 条')
    await wrapper.find('.refuted-findings summary').trigger('click')
    expect(wrapper.find('.refuted-findings').text()).toContain('比较的是哈希值')

    let downloaded = ''
    const NativeURL = URL
    vi.stubGlobal('URL', class extends NativeURL {
      static createObjectURL = vi.fn((blob: Blob) => {
        const reader = new FileReader()
        reader.onload = () => { downloaded = String(reader.result ?? '') }
        reader.readAsText(blob)
        return 'blob:review-state'
      })
      static revokeObjectURL = vi.fn()
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await button('下载报告').trigger('click')
    await vi.waitFor(() => expect(downloaded).toContain('比较的是哈希值'))
    expect(downloaded).toContain('已证伪候选 (不计入有效发现及风险评分)')
    expect(downloaded).toContain('已证伪候选')
    expect(downloaded).toContain('安全评分（越高越安全）: **92/100**')
    expect(downloaded).not.toContain('- 风险评分:')
    expect(api.scanProject).toHaveBeenCalledOnce()
  })

  it('40 条大样本复现生产报告的汇总比例，严重度只统计 20 条未被证伪候选', async () => {
    const makeFinding = (index: number, severity: string, verification: 'confirmed' | 'refuted') => ({
      title: `样本候选 ${index + 1}`, category: '认证', owasp: '', cwe: '', severity,
      file_path: `auth-${index}.py`, file_id: null, lines: `L${index + 1}`,
      line_number: index + 1, end_line: index + 1, evidence: `evidence-${index}`,
      exploit_scenario: '', fix_suggestion: '', references: [], confidence: 0.6,
      source: 'llm', verification,
      verification_reason: verification === 'refuted' ? '证据显示受输入校验保护。' : '',
    })
    const activeSeverities = [
      ...Array(7).fill('严重'), ...Array(6).fill('高'),
      ...Array(6).fill('中'), '低',
    ]
    const rejectedSeverities = [...Array(13).fill('严重'), ...Array(7).fill('高')]
    const findings = [
      ...activeSeverities.map((severity, index) => makeFinding(index, severity, 'confirmed')),
      ...rejectedSeverities.map((severity, index) => makeFinding(index + 20, severity, 'refuted')),
    ]
    api.scanProject.mockResolvedValueOnce(scanResult({
      findings,
      risk_score: 0,
      summary: '未被证伪候选 7 处严重 / 6 处高危 / 6 处中危 / 1 处低危；安全评分 0/100（越高越安全）。',
    }))
    await renderModal({ autoStart: true })

    expect(wrapper.findAll('.finding-row')).toHaveLength(20)
    expect(wrapper.find('.sev-严重 .cell-num').text()).toBe('7')
    expect(wrapper.find('.sev-高 .cell-num').text()).toBe('6')
    expect(wrapper.find('.sev-中 .cell-num').text()).toBe('6')
    expect(wrapper.find('.sev-低 .cell-num').text()).toBe('1')
    expect(wrapper.find('.refuted-findings summary').text()).toContain('已证伪候选 20 条')
    expect(wrapper.findAll('.refuted-findings > ul > li')).toHaveLength(20)
    expect(wrapper.find('.sec-summary').text()).toContain('未被证伪候选 7 处严重 / 6 处高危 / 6 处中危 / 1 处低危')
    expect(wrapper.find('.review-status-note').text()).toContain('已证伪并从风险统计排除 20 条')
  })

  it('空响应进入失败态而不是发布完成事件', async () => {
    api.scanProject.mockResolvedValue(null)
    await renderModal({ autoStart: true })
    expect(wrapper.get('[role="alert"]').text()).toContain('未返回有效结果')
    expect(wrapper.emitted('completed')).toBeUndefined()
  })
})


describe('扫描权限防御', () => {
  it.each(['file', 'task', 'project', 'all-projects'])('%s 无扫描权限时自动启动、按钮与直接处理函数均不发送请求', async (source) => {
    useUserStore().permissions = new Set()
    await renderModal({ source, autoStart: true })
    const starts = wrapper.findAll('button').filter(item => item.text() === '开始扫描')
    expect(starts).toHaveLength(2)
    for (const start of starts) {
      expect(start.attributes('disabled')).toBeDefined()
      await start.trigger('click')
    }
    await (wrapper.vm as unknown as { runScan(): Promise<void> }).runScan()
    await wrapper.setProps({ refId: 8 })
    await flushPromises()
    for (const request of Object.values(api)) expect(request).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('当前账号没有安全扫描权限')
  })

  it('失败后撤销扫描权限，重试不可执行；恢复权限可真实重试', async () => {
    api.scanProject.mockRejectedValueOnce({ message: '暂时失败' })
    await renderModal({ autoStart: true })
    useUserStore().permissions = new Set()
    await flushPromises()
    expect(button('重试扫描').attributes('disabled')).toBeDefined()
    await (wrapper.vm as unknown as { runScan(): Promise<void> }).runScan()
    expect(api.scanProject).toHaveBeenCalledOnce()
    useUserStore().permissions = new Set(['security:scan'])
    await flushPromises()
    await button('重试扫描').trigger('click')
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledTimes(2)
  })
  it('旧范围请求结束时权限已撤销，不自动发起新范围扫描', async () => {
    const pending = deferredResult()
    api.scanProject.mockReturnValueOnce(pending.promise)
    await renderModal({ autoStart: true })
    useUserStore().permissions = new Set()
    await wrapper.setProps({ refId: 8 })
    pending.resolve(scanResult())
    await flushPromises()
    expect(api.scanProject).toHaveBeenCalledOnce()
    expect(wrapper.emitted('completed')).toBeUndefined()
  })

})
