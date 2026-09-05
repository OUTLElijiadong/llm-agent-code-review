import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
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

async function renderModal(props: Record<string, unknown> = {}, realTransition = false) {
  wrapper = mount(SecurityScanModal, {
    props: { modelValue: true, source: 'project', refId: 7, refName: '测试项目', ...props },
    attachTo: document.body,
    global: { plugins: [ElementPlus], stubs: { transition: !realTransition } },
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
    ['all-projects', 'scanAllProjects', { top_n_per_project: 50, trace_dataflow: true }],
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

  it('空响应进入失败态而不是发布完成事件', async () => {
    api.scanProject.mockResolvedValue(null)
    await renderModal({ autoStart: true })
    expect(wrapper.get('[role="alert"]').text()).toContain('未返回有效结果')
    expect(wrapper.emitted('completed')).toBeUndefined()
  })
})
