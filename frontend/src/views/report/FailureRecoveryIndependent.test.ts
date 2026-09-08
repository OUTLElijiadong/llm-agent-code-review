import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
const api = vi.hoisted(() => ({ getReportDetail: vi.fn(), generateReport: vi.fn(), previewReport: vi.fn(), exportReport: vi.fn(), analyzeFolder: vi.fn() }))
vi.mock('@/api/report', () => api)
vi.mock('@/api/project', () => ({ analyzeFolder: api.analyzeFolder }))
vi.mock('@/api/review', () => ({ getTaskIssues: vi.fn().mockResolvedValue({ items: [] }) }))
vi.mock('vue-router', async original => ({ ...await original<typeof import('vue-router')>(), useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }), useRoute: () => ({ params: { id: '162' }, query: {} }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() } }))
import ReportDetail from './ReportDetail.vue'
import ProjectForm from '../project/ProjectForm.vue'
let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
const state = () => (wrapper.vm.$ as unknown as { setupState: Record<string, any> }).setupState
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(ok => { resolve = ok }); return { promise, resolve } }
function render(component: object, props: Record<string, unknown> = {}) {
  wrapper = mount(component, { props, attachTo: document.body, global: { plugins: [ElementPlus, pinia], stubs: { BaseChart: true } } })
}
beforeEach(() => {
  vi.resetAllMocks(); pinia = createPinia(); setActivePinia(pinia)
  useUserStore().profile = { id: 104, username: 'isolated', role: 'user', status: 1 }
  useUserStore().permissions = new Set(['report:view', 'report:export:json', 'project:create'])
  api.getReportDetail.mockResolvedValue({ project: { project_name: '合成项目' }, task: { task_name: '合成报告' }, stats: { total_issues: 0 }, files: [], rules_snapshot: [] })
  URL.createObjectURL = vi.fn(() => 'blob:independent-fixture'); URL.revokeObjectURL = vi.fn()
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = ''; vi.restoreAllMocks(); vi.useRealTimers() })
describe('独立失败恢复边界', () => {
  it('报告本地click异常必须释放URL和锚点', async () => {
    api.exportReport.mockResolvedValue(new Blob(['{}']))
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error('合成本地限制') })
    render(ReportDetail); await flushPromises(); await state().handleExport('json')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:independent-fixture')
    expect(document.querySelector('a[download]')).toBeNull()
  })
  it('报告访问被403撤销后迟到HTML不得重新打开预览', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const pending = deferred<string>(); api.previewReport.mockReturnValue(pending.promise)
    render(ReportDetail); await flushPromises(); const preview = state().handlePreview()
    api.getReportDetail.mockRejectedValueOnce({ code: 40300, message: '权限撤销' }); await state().loadReport()
    pending.resolve('<p>合成旧报告</p>'); await preview; await flushPromises()
    expect(state().report).toBeNull(); expect(state().previewFallbackVisible).toBe(false); expect(state().previewFallbackHtml).toBe('')
  })
  it('卸载后迟到HTML不得创建预览资源或写入缓存', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const pending = deferred<string>(); api.previewReport.mockReturnValue(pending.promise)
    render(ReportDetail); await flushPromises(); const saved = state(); const preview = saved.handlePreview(); wrapper.unmount()
    pending.resolve('<p>合成迟到内容</p>'); await preview
    expect(saved.previewFallbackVisible).toBe(false); expect(saved.previewFallbackHtml).toBe(''); expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('拖拽异步收集期间父提交锁生效后不得补发分析', async () => {
    render(ProjectForm, { visible: true, mode: 'create', initialData: null, submitting: false }); await flushPromises(); vi.useFakeTimers()
    const file = new File(['fixture'], 'main.py')
    const entry = { isFile: true, isDirectory: false, file: (done: (file: File) => void) => done(file) }
    state().onDrop({ dataTransfer: { files: [file], items: [{ webkitGetAsEntry: () => entry }] } } as unknown as DragEvent)
    await wrapper.setProps({ submitting: true }); await vi.advanceTimersByTimeAsync(200)
    expect(api.analyzeFolder).not.toHaveBeenCalled()
  })
})
