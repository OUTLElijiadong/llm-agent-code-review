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

import ReviewTaskDetail from '@/views/review/ReviewTaskDetail.vue'

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


it('independent issue:view revocation removes cached issue data', async () => {
  review.getTaskIssues.mockResolvedValueOnce({items: [{id: 5, file_id: 31, file_name: 'private.ts', title: 'cached-private-finding', description: 'cached-private-finding', severity: '高', issue_type: 'security', status: 'unfixed'}],total: 1,page: 1,page_size: 50})
  await renderDetail()
  expect(wrapper.text()).toContain('cached-private-finding')
  review.getTaskIssues.mockRejectedValueOnce({code: 40303,message: '无问题查看权限',request_id: 'isolated-issue-revocation'})
  await vi.advanceTimersByTimeAsync(3000)
  await flushPromises()
  expect(review.getReviewTaskDetail).toHaveBeenCalledTimes(2)
  expect(wrapper.text()).not.toContain('cached-private-finding')
  expect(wrapper.findAll('.issue-row')).toHaveLength(0)
})
