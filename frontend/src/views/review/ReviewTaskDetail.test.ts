import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createHash, webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import ElementPlus from 'element-plus'
import { reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskDetailOut, TaskFileOut } from '@/types/review'
import type { VersionDetailOut } from '@/types/project'

const review = vi.hoisted(() => ({ getReviewTaskDetail: vi.fn(), getTaskIssues: vi.fn() }))
const files = vi.hoisted(() => ({ getDetail: vi.fn(), getVersion: vi.fn() }))
const navigation = vi.hoisted(() => ({ push: vi.fn(), back: vi.fn() }))
const route = reactive({ params: { id: '21' } })

vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => navigation }))
vi.mock('@/api/review', () => review)
vi.mock('@/api/codeFile', () => files)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn() } }))
vi.mock('@/components/code/CodeViewer.vue', () => ({ default: {
  props: ['code', 'language', 'isBinary'],
  template: '<pre data-testid="code-preview" :data-language="language">{{ code }}</pre>',
} }))
vi.mock('@/components/issue/IssueDetailDrawer.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/issue/AiPromptModal.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/security/SecurityScanModal.vue', () => ({ default: { template: '<div />' } }))

import ReviewTaskDetail from './ReviewTaskDetail.vue'

function taskResult(overrides: Record<string, unknown> = {}): TaskDetailOut {
  return {
    id: 21, task_name: '隔离测试审查', project_id: 7, project_name: '测试项目',
    review_type: 'full', status: 'running', total_files: 8, processed_files: 2,
    total_issues: 0, severe_issues: 0, high_issues: 0, medium_issues: 0, low_issues: 0,
    score: 100, duration_ms: 1000, create_time: '2026-09-05T00:00:00Z', model_name: 'test-model',
    files: [], agent_releases: [],
    aggregation_summary: { aggregated: 0, independently_confirmed: 0, pending_human_review: 0, unresolved_conflicts: 0, insufficient_evidence: 0 },
    coverage: { stage: 'analyzing', current_file: 'worker.ts', completed_files: 2, total_files: 8, completed_chunks: 3, total_chunks: 11 },
    ...overrides,
  } as TaskDetailOut
}

const snapshotContent = 'print("审查输入 v1 🧪")\r\n  \n'
const currentContent = 'export const revision = "当前 v2，不是审查输入"'

function snapshotFile(overrides: Record<string, unknown> = {}): TaskFileOut {
  return {
    file_id: 31, project_id: 7, file_name: 'source.py', file_path: 'src/source.py',
    language: 'python', line_count: 2, version_no: 1,
    snapshot_verified: true,
    content_sha256: createHash('sha256').update(snapshotContent, 'utf8').digest('hex'),
    ...overrides,
  } as TaskFileOut
}

function versionResult(overrides: Partial<VersionDetailOut> = {}): VersionDetailOut {
  return { file_id: 31, version_no: 1, content: snapshotContent, create_time: '2026-09-05T00:00:00Z', ...overrides }
}

function deferred<Value>() {
  let resolve!: (value: Value) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Value>((finish, fail) => { resolve = finish; reject = fail })
  return { promise, resolve, reject }
}

let wrapper: VueWrapper

async function renderDetail() {
  wrapper = mount(ReviewTaskDetail, { attachTo: document.body, global: { plugins: [ElementPlus] } })
  await flushPromises()
}

function button(label: string) {
  const match = wrapper.findAll('button').find((item) => item.text() === label)
  if (!match) throw new Error(`未找到按钮：${label}`)
  return match
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('TextEncoder', TextEncoder)
  route.params.id = '21'
  review.getReviewTaskDetail.mockResolvedValue(taskResult())
  review.getTaskIssues.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 })
  files.getVersion.mockReset().mockResolvedValue(versionResult())
  files.getDetail.mockReset().mockResolvedValue({
    id: 31, file_name: 'source-current.ts', language: 'typescript', version_no: 2,
    is_binary: 0, content: currentContent,
  })
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('审查输入快照预览', () => {
  beforeEach(() => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status: 'success', files: [snapshotFile()] }))
  })

  async function selectFile(index = 0) {
    await wrapper.findAll('.file-row')[index].trigger('keydown', { key: 'Enter' })
    await flushPromises()
  }

  async function expectSnapshot() {
    await vi.waitFor(() => expect(wrapper.find('[data-testid="code-preview"]').exists()).toBe(true))
    expect(wrapper.get('[data-testid="code-preview"]').element.textContent).toBe(snapshotContent)
    expect(wrapper.get('.code-provenance').text()).toContain('审查输入 v1 / 快照')
    expect(wrapper.get('.code-provenance').text()).toContain('SHA-256 已校验')
    expect(files.getDetail).not.toHaveBeenCalled()
  }

  function expectBlocked(reason: string) {
    expect(wrapper.get('.code-error[role="alert"]').text()).toContain(reason)
    expect(wrapper.get('.code-error').text()).toContain('不会改用当前文件')
    expect(wrapper.find('[data-testid="code-preview"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('SHA-256 已校验')
    expect(files.getDetail).not.toHaveBeenCalled()
  }

  it('当前文件已是 v2 时仍读取并校验任务 v1，使用冻结的名称和语言', async () => {
    await renderDetail()
    await selectFile()
    await expectSnapshot()
    expect(files.getVersion).toHaveBeenCalledExactlyOnceWith(31, 1)
    expect(wrapper.get('[data-testid="code-preview"]').attributes('data-language')).toBe('python')
    expect(wrapper.get('.code-file').text()).toContain('source.py')
    expect(wrapper.text()).not.toContain('source-current.ts')
    expect(wrapper.get('.code-provenance').attributes('aria-live')).toBe('polite')
  })

  it.each([false, undefined])('有快照摘要但服务端校验为 %s 时阻断，不能当作旧任务回退', async (snapshot_verified) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ files: [snapshotFile({ snapshot_verified })] }))
    await renderDetail()
    await selectFile()
    expectBlocked('完整性校验失败')
    expect(files.getVersion).not.toHaveBeenCalled()
  })

  it.each([
    { content_sha256: null }, { content_sha256: '' }, { content_sha256: 'broken' },
    { version_no: 0 }, { version_no: undefined },
  ])('快照元数据不完整或非法时拒绝当前内容兜底：%j', async (metadata) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ files: [snapshotFile(metadata)] }))
    await renderDetail()
    await selectFile()
    expectBlocked('快照元数据')
    expect(files.getVersion).not.toHaveBeenCalled()
  })

  it('服务端快照校验失败后重试先重新读取任务记录，恢复后才读取历史内容', async () => {
    review.getReviewTaskDetail.mockResolvedValueOnce(taskResult({ status: 'success', files: [snapshotFile({ snapshot_verified: false })] }))
    await renderDetail()
    await selectFile()
    expectBlocked('完整性校验失败')
    const pending = deferred<TaskDetailOut>()
    review.getReviewTaskDetail.mockReturnValueOnce(pending.promise)
    await button('重新加载代码').trigger('click')
    expect(files.getVersion).not.toHaveBeenCalled()
    expect(wrapper.get('.pane-code').attributes('aria-busy')).toBe('true')
    pending.resolve(taskResult({ status: 'success', files: [snapshotFile()] }))
    await expectSnapshot()
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
  })

  it('重新确认失败快照时任务接口断线，保留阻断和明确重试错误', async () => {
    review.getReviewTaskDetail.mockResolvedValueOnce(taskResult({ status: 'success', files: [snapshotFile({ snapshot_verified: false })] }))
    await renderDetail()
    await selectFile()
    review.getReviewTaskDetail.mockRejectedValueOnce(new Error('任务连接中断'))
    await button('重新加载代码').trigger('click')
    await flushPromises()
    expectBlocked('任务连接中断')
    expect(files.getVersion).not.toHaveBeenCalled()
    expect(wrapper.get('.pane-code').attributes('aria-busy')).toBe('false')
  })

  it('已知快照的元数据在重试中消失时仍阻断，多次重试也不能降级为旧任务', async () => {
    review.getReviewTaskDetail.mockResolvedValueOnce(taskResult({ status: 'success', files: [snapshotFile({ snapshot_verified: false })] }))
    await renderDetail()
    await selectFile()
    expectBlocked('完整性校验失败')
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status: 'success', files: [
      snapshotFile({ snapshot_verified: false, content_sha256: null }),
    ] }))
    await button('重新加载代码').trigger('click')
    await flushPromises()
    expectBlocked('完整性校验失败')
    await button('重新加载代码').trigger('click')
    await flushPromises()
    expectBlocked('完整性校验失败')
    expect(files.getVersion).not.toHaveBeenCalled()
  })

  it.each([
    { snapshot_verified: false, content_sha256: null },
    { snapshot_verified: undefined, content_sha256: undefined },
  ])('只有没有快照元数据的旧任务才显示带历史未知警告的当前文件：%j', async (metadata) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ files: [snapshotFile(metadata)] }))
    await renderDetail()
    await selectFile()
    expect(files.getDetail).toHaveBeenCalledExactlyOnceWith(31)
    expect(files.getVersion).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="code-preview"]').text()).toBe(currentContent)
    expect(wrapper.get('.code-provenance').text()).toContain('当前内容，历史输入未知')
    expect(wrapper.get('.code-provenance').text()).toContain('当前文件，无法证明当时输入')
    expect(wrapper.text()).not.toContain('SHA-256 已校验')
  })

  it('即使历史接口返回成功，UTF-8 内容摘要不符也不展示', async () => {
    files.getVersion.mockResolvedValue(versionResult({ content: snapshotContent.trim() }))
    await renderDetail()
    await selectFile()
    await vi.waitFor(() => expect(wrapper.find('.code-error').exists()).toBe(true))
    expectBlocked('SHA-256 不匹配')
  })

  it.each(['crypto', 'TextEncoder'])('缺少 %s 时明确无法校验，不把服务端标记当作浏览器验证', async (capability) => {
    vi.stubGlobal(capability, undefined)
    await renderDetail()
    await selectFile()
    expectBlocked('浏览器不支持快照完整性校验')
  })

  it('浏览器摘要计算异常显示错误而不是信任未验证内容', async () => {
    vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockRejectedValue(new Error('摘要计算失败')) } })
    await renderDetail()
    await selectFile()
    expectBlocked('摘要计算失败')
  })

  it.each([{ file_id: 99 }, { version_no: 2 }, { content: undefined }])('版本接口返回的身份或内容无效时拒绝展示：%j', async (version) => {
    files.getVersion.mockResolvedValue(versionResult(version))
    await renderDetail()
    await selectFile()
    expectBlocked('历史版本响应')
  })

  it('软删除或历史版本 404 明确无法读取原始输入，重试仍读取同一历史版本', async () => {
    files.getVersion.mockRejectedValueOnce({ code: 40400, message: '文件不存在或已删除' })
    await renderDetail()
    await selectFile()
    expectBlocked('无法读取原始审查输入')
    expect(wrapper.get('.code-error').text()).toContain('文件不存在或已删除')
    await button('重新加载代码').trigger('click')
    await expectSnapshot()
    expect(files.getVersion).toHaveBeenCalledTimes(2)
    expect(files.getVersion).toHaveBeenLastCalledWith(31, 1)
  })

  it('历史请求未完成时反馈忙态，不宣称已校验，同一文件键盘连按不重复请求', async () => {
    const pending = deferred<VersionDetailOut>()
    files.getVersion.mockReturnValueOnce(pending.promise)
    await renderDetail()
    await selectFile()
    await selectFile()
    expect(files.getVersion).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.pane-code').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('.code-provenance').text()).toContain('读取并校验')
    expect(wrapper.text()).not.toContain('SHA-256 已校验')
    expect(wrapper.find('[data-testid="code-preview"]').exists()).toBe(false)
    pending.resolve(versionResult())
    await expectSnapshot()
    expect(wrapper.get('.pane-code').attributes('aria-busy')).toBe('false')
  })

  it('源码编辑入口明确编辑当前版本，导航不冒充快照编辑', async () => {
    await renderDetail()
    const edit = button('编辑当前版本')
    expect(edit.attributes('title')).toContain('快照')
    await edit.trigger('click')
    expect(navigation.push).toHaveBeenCalledWith('/code/7/file/31')
    expect(files.getDetail).not.toHaveBeenCalled()
    expect(files.getVersion).not.toHaveBeenCalled()
  })

  it.each(['resolve', 'reject'] as const)('切换文件后忽略旧历史请求的迟到 %s，不覆盖新内容或错误', async (outcome) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status: 'success', files: [
      snapshotFile(), snapshotFile({ file_id: 32, file_name: 'second.py' }),
    ] }))
    const pending = deferred<VersionDetailOut>()
    files.getVersion.mockReturnValueOnce(pending.promise).mockResolvedValue(versionResult({ file_id: 32 }))
    await renderDetail()
    await selectFile()
    await selectFile(1)
    await expectSnapshot()
    if (outcome === 'resolve') pending.resolve(versionResult({ content: '不应出现的旧响应' }))
    else pending.reject(new Error('不应出现的旧错误'))
    await flushPromises()
    expect(wrapper.get('.code-file').text()).toContain('second.py')
    expect(wrapper.get('[data-testid="code-preview"]').element.textContent).toBe(snapshotContent)
    expect(wrapper.find('.code-error').exists()).toBe(false)
  })

  it('摘要计算期间切换任务，旧摘要完成后不能污染新任务预览', async () => {
    const pending = deferred<ArrayBuffer>()
    const digest = vi.fn().mockReturnValue(pending.promise)
    vi.stubGlobal('crypto', { subtle: { digest } })
    await renderDetail()
    await selectFile()
    expect(digest).toHaveBeenCalledTimes(1)
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ id: 22, status: 'success', files: [
      snapshotFile({ snapshot_verified: false, content_sha256: null }),
    ] }))
    route.params.id = '22'
    await flushPromises()
    await selectFile()
    pending.resolve(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(snapshotContent)))
    await flushPromises()
    expect(wrapper.get('[data-testid="code-preview"]').text()).toBe(currentContent)
    expect(wrapper.get('.code-provenance').text()).toContain('历史输入未知')
    expect(wrapper.text()).not.toContain('SHA-256 已校验')
  })
})

describe('审查详情真实执行状态', () => {
  it('沙箱历史16字段行按报告4条展示，未分级不伪造零风险', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({
      id: 161, review_type: 'sandbox_test', status: 'success', total_files: 1, processed_files: 1,
      total_issues: 4, model_name: null, coverage: null,
      report_issue_summary: { source: 'sandbox_report', basis: 'report_headings', total: 4, unclassified: 4,
        severity_counts: { 严重: 0, 高: 0, 中: 0, 低: 0 }, structured_issues: 0 },
    }))
    await renderDetail()
    const panel = wrapper.get('[aria-label="执行阶段与覆盖"]')
    expect(panel.text()).toContain('已完成')
    expect(panel.text()).toContain('1 / 1')
    expect(panel.text()).not.toContain('未知')
    expect(wrapper.get('.head-tally').text()).toContain('4未分级')
    expect(wrapper.get('.head-tally').text()).toContain('4报告条目')
    expect(wrapper.get('.head-tally').text()).not.toContain('0危急')
    expect(wrapper.text()).toContain('条目数不代表已确认漏洞数')
    expect(wrapper.text()).toContain('没有结构化问题明细')
    expect(wrapper.get('.score-status').text()).toContain('不代表安全风险评级')
    expect(wrapper.find('.workbench').exists()).toBe(false)
  })

  it('沙箱报告缺少问题清单时保留未知条目数，不显示旧占位16或0', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({
      review_type: 'sandbox_test', status: 'success', total_issues: 16, coverage: null,
      report_issue_summary: { source: 'sandbox_report', basis: 'unavailable', total: null, unclassified: null,
        severity_counts: { 严重: 0, 高: 0, 中: 0, 低: 0 }, structured_issues: 0 },
    }))
    await renderDetail()
    expect(wrapper.get('.head-tally').text()).toContain('—报告条目')
    expect(wrapper.get('.head-tally').text()).not.toContain('16')
  })

  it('只显示 coverage 返回的阶段、文件和分片计数，不推算百分比', async () => {
    await renderDetail()
    const panel = wrapper.get('[aria-label="执行阶段与覆盖"]')
    expect(panel.text()).toContain('分析中')
    expect(panel.text()).toContain('worker.ts')
    expect(panel.text()).toContain('2 / 8')
    expect(panel.text()).toContain('3 / 11')
    expect(panel.text()).toContain('test-model')
    expect(panel.text()).not.toMatch(/\d+\s*%/)
    expect(panel.find('[role="status"]').attributes('aria-live')).toBe('polite')
  })

  it.each(['pending', 'running', 'failed', 'cancelled'])('%s 即使携带 100 分也不显示风险评级和满分动画', async (status) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status }))
    await renderDetail()
    await vi.advanceTimersByTimeAsync(1300)
    expect(wrapper.find('.score-orb').exists()).toBe(false)
    expect(wrapper.find('.score-status').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('优秀 · 可发布')
    expect(wrapper.text()).toContain('尚无最终评分')
  })

  it('成功且评分有效时显示服务端原值，不经动画制造中间评分', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status: 'success', score: 87 }))
    await renderDetail()
    expect(wrapper.get('.score-val').text()).toBe('87')
    expect(wrapper.find('.score-status').exists()).toBe(true)
  })

  it.each([null, undefined, { stage: null, completed_files: -1, total_files: '8', completed_chunks: null }])('历史或非法 coverage 标未知，不借用任务总数', async (coverage) => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ coverage, model_name: null }))
    await renderDetail()
    const panel = wrapper.get('[aria-label="执行阶段与覆盖"]')
    expect(panel.text()).toContain('阶段未知')
    expect(panel.text()).toContain('未知 / 未知')
    expect(panel.text()).not.toContain('2 / 8')
    expect(wrapper.text()).toContain('模型未知')
    expect(wrapper.text()).not.toContain('DeepSeek V4')
  })

  it('真实零计数保留为零，而不是未知或虚构完成', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({
      coverage: { stage: 'queued', completed_files: 0, total_files: 8, completed_chunks: 0, total_chunks: 11 },
    }))
    await renderDetail()
    const panel = wrapper.get('[aria-label="执行阶段与覆盖"]')
    expect(panel.text()).toContain('等待执行')
    expect(panel.text()).toContain('0 / 8')
    expect(panel.text()).toContain('0 / 11')
  })

  it('失败展示真实覆盖缺口和服务端错误，不伪装为未发现问题', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({
      status: 'failed', coverage: { stage: 'failed', error: '第 4 个分片模型请求失败', completed_chunks: 3, total_chunks: 11 },
    }))
    await renderDetail()
    expect(wrapper.get('.execution-error[role="alert"]').text()).toContain('第 4 个分片模型请求失败')
    expect(wrapper.text()).toContain('3 / 11')
    expect(wrapper.text()).not.toContain('没有匹配的问题')
  })

  it('初次连接失败有原因和重试入口，重试只读取原任务', async () => {
    review.getReviewTaskDetail.mockRejectedValueOnce({ message: '连接超时' })
    await renderDetail()
    expect(wrapper.get('.page-error[role="alert"]').text()).toContain('连接超时')
    await button('重新加载').trigger('click')
    await flushPromises()
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
    expect(review.getReviewTaskDetail).toHaveBeenLastCalledWith(21)
    expect(wrapper.find('.page-error').exists()).toBe(false)
  })

  it('轮询失败保留上次数据、停止静默重试，手动重连成功后继续到终态', async () => {
    await renderDetail()
    review.getReviewTaskDetail.mockRejectedValueOnce({ message: '网络连接中断' })
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.get('.connection-error[role="alert"]').text()).toContain('网络连接中断')
    expect(wrapper.text()).toContain('上次成功获取')
    expect(wrapper.text()).toContain('3 / 11')
    await vi.advanceTimersByTimeAsync(9000)
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ status: 'success', score: 78, coverage: { stage: 'complete', completed_files: 8, total_files: 8 } }))
    await button('重新获取状态').trigger('click')
    await flushPromises()
    expect(wrapper.find('.connection-error').exists()).toBe(false)
    expect(wrapper.get('.score-val').text()).toBe('78')
    await vi.advanceTimersByTimeAsync(9000)
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(3)
  })

  it('问题接口失败不显示空成功态，可独立重试', async () => {
    review.getTaskIssues.mockRejectedValueOnce({ message: '问题列表暂不可用' })
    await renderDetail()
    expect(wrapper.get('.issues-error[role="alert"]').text()).toContain('问题列表暂不可用')
    expect(wrapper.text()).not.toContain('没有匹配的问题')
    await button('重新加载问题').trigger('click')
    await flushPromises()
    expect(review.getTaskIssues).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.issues-error').exists()).toBe(false)
  })

  it('忙态重试只发一次读取，卸载后的迟到响应不会重启轮询', async () => {
    review.getReviewTaskDetail.mockRejectedValueOnce(new Error('连接失败'))
    await renderDetail()
    let finish!: (value: TaskDetailOut) => void
    review.getReviewTaskDetail.mockReturnValue(new Promise<TaskDetailOut>((resolve) => { finish = resolve }))
    await Promise.all([button('重新加载').trigger('click'), button('重新加载').trigger('click')])
    await flushPromises()
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    finish(taskResult())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9000)
    expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
  })

  it('切换任务后丢弃旧任务的迟到响应', async () => {
    let finishOld!: (value: TaskDetailOut) => void
    review.getReviewTaskDetail.mockReturnValueOnce(new Promise<TaskDetailOut>((resolve) => { finishOld = resolve }))
    await renderDetail()
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ id: 22, task_name: '新任务', status: 'success' }))
    route.params.id = '22'
    await flushPromises()
    finishOld(taskResult({ task_name: '旧任务不应展示' }))
    await flushPromises()
    expect(review.getReviewTaskDetail).toHaveBeenLastCalledWith(22)
    expect(wrapper.text()).toContain('新任务')
    expect(wrapper.text()).not.toContain('旧任务不应展示')
  })

  it('代码连接错误可重试，文件行支持键盘选择', async () => {
    review.getReviewTaskDetail.mockResolvedValue(taskResult({ files: [
      { file_id: 31, project_id: 7, file_name: 'worker.ts', language: 'typescript', line_count: 2, version_no: 1 },
    ] }))
    files.getDetail.mockRejectedValueOnce({ message: '代码读取超时' }).mockResolvedValue({
      id: 31, file_name: 'worker.ts', language: 'typescript', is_binary: 0, content: 'export const ready = true',
    })
    await renderDetail()
    await wrapper.get('.file-row[role="button"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.get('.code-error[role="alert"]').text()).toContain('代码读取超时')
    await button('重新加载代码').trigger('click')
    await flushPromises()
    expect(files.getDetail).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.code-error').exists()).toBe(false)
  })

  it('迟到的问题筛选响应不能覆盖较新的筛选结果', async () => {
    await renderDetail()
    let finishOld!: (value: unknown) => void
    review.getTaskIssues.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve }))
    await wrapper.findAll('button.chip')[0].trigger('click')
    review.getTaskIssues.mockResolvedValue({ items: [], total: 0 })
    await wrapper.findAll('button.chip')[1].trigger('click')
    await flushPromises()
    finishOld({ items: [{ id: 5, title: '旧筛选不应展示', description: '旧结果', severity: '高', issue_type: '安全漏洞', status: 'unfixed' }], total: 1 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧筛选不应展示')
  })
})
