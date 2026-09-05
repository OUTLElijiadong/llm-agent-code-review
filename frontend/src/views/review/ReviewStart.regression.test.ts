import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ElCheckbox, ElRadio, ElRadioButton } from 'element-plus'
import { codeFile, deferred, pageOf, project, scanMountOptions } from './scanRegressionTestUtils'

const api = vi.hoisted(() => ({ projects: vi.fn(), files: vi.fn(), start: vi.fn(), discuss: vi.fn(), confirm: vi.fn() }))
const router = vi.hoisted(() => ({ push: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/api/codeFile', () => ({ list: api.files }))
vi.mock('@/api/review', () => ({ startReview: api.start }))
vi.mock('@/api/discussion', () => ({ startDiscussion: api.discuss }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))
import ReviewStart from './ReviewStart.vue'

let wrapper: VueWrapper
beforeEach(() => {
  vi.resetAllMocks()
  api.projects.mockResolvedValue(pageOf([project(1)]))
  api.files.mockResolvedValue(pageOf([codeFile(1)]))
  api.start.mockResolvedValue({ task_id: 7, status: 'pending' })
  api.discuss.mockResolvedValue({ session_id: 'discussion', agents: [], file_name: 'file1.py', ws_url: '/ws/stub' })
  api.confirm.mockResolvedValue(true)
  router.push.mockResolvedValue(undefined)
})
afterEach(() => wrapper?.unmount())

async function render() {
  wrapper = mount(ReviewStart, scanMountOptions)
  await flushPromises()
  return wrapper.vm as any
}

async function chooseProject(vm: any, id = 1) {
  vm.form.project_id = id
  await vm.onProjectChange(id)
  await flushPromises()
}

describe('审查扫描范围失败回归', () => {
  it('只有全部项目范围显示批量项目摘要', async () => {
    const vm = await render()
    expect(wrapper.text()).not.toContain('将审查全部')
    vm.form.scope = 'files'
    await flushPromises()
    expect(wrapper.text()).not.toContain('将审查全部')
    vm.form.scope = 'all'
    await flushPromises()
    expect(wrapper.text()).toContain('将审查全部')
  })

  it('R1 快速切换时旧响应不能覆盖新项目文件', async () => {
    const vm = await render()
    const older = deferred<ReturnType<typeof pageOf>>()
    const newer = deferred<ReturnType<typeof pageOf>>()
    api.files.mockImplementation(({ project_id }) => project_id === 1 ? older.promise : newer.promise)
    vm.form.project_id = 1
    const first = vm.onProjectChange(1)
    vm.form.project_id = 2
    const second = vm.onProjectChange(2)
    newer.resolve(pageOf([codeFile(202, { project_id: 2 })]))
    await second
    older.resolve(pageOf([codeFile(101)]))
    await first
    expect(vm.files.map((file: any) => file.id)).toEqual([202])
  })

  it('旧请求先结束时不能解除新请求的加载锁', async () => {
    const vm = await render()
    const older = deferred<ReturnType<typeof pageOf>>()
    const newer = deferred<ReturnType<typeof pageOf>>()
    api.files.mockImplementation(({ project_id }) => project_id === 1 ? older.promise : newer.promise)
    vm.form.project_id = 1
    const first = vm.onProjectChange(1)
    vm.form.project_id = 2
    const second = vm.onProjectChange(2)
    older.resolve(pageOf([codeFile(101)]))
    await first
    expect(vm.loadingFiles).toBe(true)
    expect(vm.submitDisabled).toBe(true)
    newer.resolve(pageOf([codeFile(202, { project_id: 2 })]))
    await second
  })

  it('重置使在途文件请求失效', async () => {
    const vm = await render()
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.files.mockReturnValue(pending.promise)
    vm.form.project_id = 1
    const request = vm.onProjectChange(1)
    vm.onReset()
    pending.resolve(pageOf([codeFile(1)]))
    await request
    expect(vm.files).toEqual([])
    expect(vm.loadingFiles).toBe(false)
  })

  it('R2 完整读取501文件，整个项目超限时明确阻断而非截断', async () => {
    const vm = await render()
    api.files.mockImplementation(({ page = 1 }) => Promise.resolve(page === 1
      ? pageOf(Array.from({ length: 500 }, (_, index) => codeFile(index + 1)), 501, 1, 500)
      : pageOf([codeFile(501)], 501, 2, 500)))
    await chooseProject(vm)
    expect(api.files).toHaveBeenCalledTimes(2)
    expect(vm.files).toHaveLength(501)
    expect(vm.submitDisabled).toBe(true)
    expect(wrapper.text()).toContain('超过单任务上限 500')
    await vm.onSubmit()
    expect(api.start).not.toHaveBeenCalled()
  })

  it('第二页失败不得把第一页当成完整范围；可以重试', async () => {
    const vm = await render()
    api.files.mockResolvedValueOnce(pageOf([codeFile(1)], 2, 1, 1)).mockRejectedValueOnce(new Error('第二页读取失败'))
    await chooseProject(vm)
    expect(vm.files).toEqual([])
    expect(vm.submitDisabled).toBe(true)
    expect(wrapper.text()).toContain('第二页读取失败')
    const retry = wrapper.findAll('button').find(button => button.text().includes('重试文件'))
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(vm.files).toHaveLength(1)
    expect(vm.submitDisabled).toBe(false)
  })

  it('R5 全部项目读取101个而不是第一页100个', async () => {
    api.projects.mockImplementation(({ page = 1 }) => Promise.resolve(page === 1
      ? pageOf(Array.from({ length: 100 }, (_, index) => project(index + 1)), 101, 1, 100)
      : pageOf([project(101)], 101, 2, 100)))
    const vm = await render()
    expect(vm.projects).toHaveLength(101)
    expect(api.projects).toHaveBeenCalledTimes(2)
  })

  it('项目第二页失败时禁止使用残缺项目范围，显示重试入口', async () => {
    api.projects.mockResolvedValueOnce(pageOf([project(1)], 2, 1, 1)).mockRejectedValueOnce(new Error('项目列表断线'))
    const vm = await render()
    vm.form.scope = 'all'
    await flushPromises()
    expect(vm.projects).toEqual([])
    expect(vm.submitDisabled).toBe(true)
    expect(wrapper.text()).toContain('项目列表断线')
    expect(wrapper.text()).toContain('重试项目')
  })

  it.each([
    { size_bytes: 0, is_reviewable: false },
    { size_bytes: 3, is_reviewable: false },
    { size_bytes: 10, is_reviewable: true, is_binary: 1 },
    { size_bytes: 10, is_reviewable: undefined },
  ])('R3 已知空白、二进制或未验证文件不能进入扫描 %j', async (metadata) => {
    const vm = await render()
    api.files.mockResolvedValue(pageOf([codeFile(1, metadata)]))
    await chooseProject(vm)
    expect(vm.submitDisabled).toBe(true)
    await vm.onSubmit()
    expect(api.start).not.toHaveBeenCalled()
  })

  it('保留空标记文件展示，但全选只包含有效非空文件', async () => {
    const vm = await render()
    api.files.mockResolvedValue(pageOf([codeFile(1), codeFile(2, { size_bytes: 0, is_reviewable: false })]))
    await chooseProject(vm)
    vm.form.scope = 'files'
    await flushPromises()
    await wrapper.find('.select-all-checkbox input').setValue(true)
    expect(vm.form.file_ids).toEqual([1])
    expect(wrapper.text()).toContain('file2.py')
    const empty = wrapper.findAllComponents(ElCheckbox).find(checkbox => checkbox.props('value') === 2)
    expect(empty?.props('disabled')).toBe(true)
  })

  it('后端标识纯Unicode空白不可审查时按原布尔字段排除，并明确保留存储原因', async () => {
    const vm = await render()
    api.files.mockResolvedValue(pageOf([
      codeFile(1, { file_name: 'unicode-space.py', size_bytes: 9, is_reviewable: false }),
      codeFile(2),
    ]))
    await chooseProject(vm)
    expect(wrapper.text()).toContain('unicode-space.py：空白或无有效审查内容，保留存储但不参与扫描')
    expect(vm.files).toHaveLength(2)
    await vm.onSubmit()
    expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ file_ids: [2] }))
  })

  it('手动选择超限或旧项目文件ID必须阻断，不能切片或过滤后提交', async () => {
    const vm = await render()
    await chooseProject(vm)
    vm.form.scope = 'files'
    vm.form.file_ids = [1, 999]
    await vm.onSubmit()
    expect(api.start).not.toHaveBeenCalled()
    vm.form.file_ids = Array.from({ length: 501 }, (_, index) => index + 1)
    await vm.onSubmit()
    expect(api.start).not.toHaveBeenCalled()
  })

  it('R4 选择圆桌时转为显式单选并清除旧多选，不默认取第一项', async () => {
    const vm = await render()
    api.files.mockResolvedValue(pageOf([codeFile(1), codeFile(2)]))
    await chooseProject(vm)
    vm.form.file_ids = [1, 2]
    const discussion = wrapper.findAllComponents(ElRadio).find(radio => radio.props('value') === 'discuss')!
    await discussion.find('input').setValue(true)
    expect(vm.form.scope).toBe('files')
    expect(vm.form.file_ids).toEqual([])
    expect(wrapper.text()).toContain('圆桌讨论仅支持单文件')
    expect(wrapper.findAllComponents(ElRadioButton).find(radio => radio.props('value') === 'whole')?.props('disabled')).toBe(true)
    await vm.onSubmit()
    expect(api.discuss).not.toHaveBeenCalled()
    const target = wrapper.findAllComponents(ElRadio).find(radio => radio.props('value') === 2)!
    await target.find('input').setValue(true)
    await vm.onSubmit()
    expect(api.discuss).toHaveBeenCalledWith(expect.objectContaining({ file_id: 2 }))
  })

  it('绕过UI的多文件圆桌调用也不得静默取第一项', async () => {
    const vm = await render()
    api.files.mockResolvedValue(pageOf([codeFile(1), codeFile(2)]))
    await chooseProject(vm)
    vm.form.review_type = 'discuss'
    vm.form.scope = 'files'
    vm.form.file_ids = [1, 2]
    await vm.onSubmit()
    expect(api.discuss).not.toHaveBeenCalled()
  })

  it('提交校验和慢请求期间阻止重复提交、锁定输入并只显示真实提交阶段', async () => {
    const vm = await render()
    await chooseProject(vm)
    const pending = deferred<{ task_id: number; status: string }>()
    api.start.mockReturnValue(pending.promise)
    const first = vm.onSubmit()
    const second = vm.onSubmit()
    await flushPromises()
    expect(api.start).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('正在提交审查任务')
    expect(wrapper.text()).not.toContain('正在审查中')
    expect(wrapper.findAll('button').find(button => button.text() === '重置')?.attributes('disabled')).toBeDefined()
    pending.resolve({ task_id: 8, status: 'pending' })
    await Promise.all([first, second])
    await flushPromises()
    expect(router.push).toHaveBeenCalledWith('/reviews/8')
  })

  it('R6 批量空项目、请求失败、成功均推进数量并保留各自原因', async () => {
    api.projects.mockResolvedValue(pageOf([project(1), project(2), project(3)]))
    const vm = await render()
    vm.form.scope = 'all'
    api.files.mockImplementation(({ project_id }) => Promise.resolve(pageOf(project_id === 1 ? [] : [codeFile(project_id, { project_id })])))
    const last = deferred<{ task_id: number; status: string }>()
    api.start.mockImplementation(({ project_id }) => project_id === 2 ? Promise.reject(new Error('额度不足')) : last.promise)
    const submitting = vm.onSubmit()
    await flushPromises()
    expect(wrapper.text()).toContain('已处理 2 / 3')
    expect(wrapper.text()).toContain('额度不足')
    last.resolve({ task_id: 9, status: 'pending' })
    await submitting
    await flushPromises()
    expect(wrapper.text()).toContain('已处理 3 / 3')
    expect(wrapper.text()).toContain('成功 1')
    expect(wrapper.text()).toContain('失败 1')
    expect(wrapper.text()).toContain('跳过 1')
    expect(wrapper.text()).toContain('无有效非空文件')
    expect(router.push).not.toHaveBeenCalled()
  })

  it('旧项目请求失败不会污染新项目的文件或错误状态', async () => {
    const vm = await render()
    const older = deferred<ReturnType<typeof pageOf>>()
    api.files.mockReturnValueOnce(older.promise).mockResolvedValueOnce(pageOf([codeFile(2, { project_id: 2 })]))
    vm.form.project_id = 1
    const request = vm.onProjectChange(1)
    await chooseProject(vm, 2)
    older.reject(new Error('旧项目失败'))
    await request
    expect(vm.files.map((file: any) => file.id)).toEqual([2])
    expect(vm.filesError).toBe('')
  })

  it.each([
    [pageOf([codeFile(1)], 2, 1, 1), pageOf([codeFile(1)], 2, 2, 1)],
    [pageOf([codeFile(1)], 2, 1, 1), pageOf([], 2, 2, 1)],
    [pageOf([codeFile(1)], 2, 1, 1), pageOf([codeFile(2)], 3, 2, 1)],
    [{ items: [codeFile(1)] }, pageOf([])],
  ])('异常分页必须阻断，不能去重或忽略缺失项后假称完整 %#', async (first, second) => {
    const vm = await render()
    api.files.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    await chooseProject(vm)
    expect(vm.files).toEqual([])
    expect(vm.filesError).toContain('未使用部分结果')
    await vm.onSubmit()
    expect(api.start).not.toHaveBeenCalled()
    expect(api.files.mock.calls.length).toBeLessThanOrEqual(2)
  })

  it('500个有效文件加一个空标记仍完整翻页并提交全部500个有效文件', async () => {
    const vm = await render()
    api.files.mockResolvedValueOnce(pageOf(Array.from({ length: 500 }, (_, index) => codeFile(index + 1)), 501, 1, 500))
      .mockResolvedValueOnce(pageOf([codeFile(501, { size_bytes: 0, is_reviewable: false })], 501, 2, 500))
    await chooseProject(vm)
    expect(vm.files).toHaveLength(501)
    expect(vm.submitDisabled).toBe(false)
    await vm.onSubmit()
    expect(api.start.mock.calls[0][0].file_ids).toEqual(Array.from({ length: 500 }, (_, index) => index + 1))
    expect(wrapper.text()).toContain('file501.py')
  })

  it('超过500个可选文件时仍可明确选择后续页文件，不自动截取前500项', async () => {
    const vm = await render()
    api.files.mockResolvedValueOnce(pageOf(Array.from({ length: 500 }, (_, index) => codeFile(index + 1)), 501, 1, 500))
      .mockResolvedValueOnce(pageOf([codeFile(501)], 501, 2, 500))
    await chooseProject(vm)
    vm.form.scope = 'files'
    await flushPromises()
    expect(wrapper.find('.select-all-checkbox input').attributes('disabled')).toBeDefined()
    const target = wrapper.findAllComponents(ElCheckbox).find(checkbox => checkbox.props('value') === 501)!
    await target.find('input').setValue(true)
    await vm.onSubmit()
    expect(api.start).toHaveBeenCalledWith(expect.objectContaining({ file_ids: [501] }))
  })

  it('全部项目实际提交包含第101个项目，不只修正显示计数', async () => {
    api.projects.mockImplementation(({ page = 1 }) => Promise.resolve(page === 1
      ? pageOf(Array.from({ length: 100 }, (_, index) => project(index + 1)), 101, 1, 100)
      : pageOf([project(101)], 101, 2, 100)))
    api.files.mockImplementation(({ project_id }) => Promise.resolve(pageOf([codeFile(project_id, { project_id })])))
    const vm = await render()
    vm.form.scope = 'all'
    await vm.onSubmit()
    expect(api.start).toHaveBeenCalledTimes(101)
    expect(api.start).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: 101, file_ids: [101] }))
    expect(wrapper.text()).toContain('已处理 101 / 101')
  })

  it('批量超限项目计为失败，空项目计为跳过，均不创建残缺任务', async () => {
    api.projects.mockResolvedValue(pageOf([project(1), project(2)]))
    const vm = await render()
    vm.form.scope = 'all'
    api.files.mockImplementation(({ project_id, page = 1 }) => Promise.resolve(project_id === 2 ? pageOf([])
      : page === 1 ? pageOf(Array.from({ length: 500 }, (_, index) => codeFile(index + 1)), 501, 1, 500)
        : pageOf([codeFile(501)], 501, 2, 500)))
    await vm.onSubmit()
    expect(vm.batchFailed).toBe(1)
    expect(vm.batchSkipped).toBe(1)
    expect(wrapper.text()).toContain('超过单任务上限 500')
    expect(api.start).not.toHaveBeenCalled()
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('只重试未提交过的读取失败项目，保留原始参数且不重复提交成功或结果未知的项目', async () => {
    api.projects.mockResolvedValue(pageOf([project(1), project(2), project(3)]))
    const vm = await render()
    vm.form.scope = 'all'
    vm.form.task_name = '原始批次'
    api.files.mockImplementation(({ project_id }) => project_id === 1 ? Promise.reject(new Error('读取连接失败'))
      : Promise.resolve(pageOf([codeFile(project_id, { project_id })])))
    api.start.mockRejectedValueOnce(new Error('创建响应丢失')).mockResolvedValue({ task_id: 9, status: 'pending' })
    await vm.onSubmit()
    expect(vm.batchResults.map((result: any) => result.retryable)).toEqual([true, false, false])
    expect(api.start).toHaveBeenCalledTimes(2)
    vm.form.task_name = '后来改名'
    api.files.mockResolvedValue(pageOf([codeFile(1)]))
    await vm.retryBatchFailures()
    expect(api.start).toHaveBeenCalledTimes(3)
    expect(api.start).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: 1, task_name: '原始批次' }))
    expect(vm.batchCreated).toBe(2)
    expect(vm.batchFailed).toBe(1)
    expect(vm.batchCompleted).toBe(3)
  })

  it('取消批量确认不会读取文件或创建任务', async () => {
    const vm = await render()
    vm.form.scope = 'all'
    api.confirm.mockResolvedValue(false)
    await vm.onSubmit()
    expect(api.files).not.toHaveBeenCalled()
    expect(api.start).not.toHaveBeenCalled()
    expect(vm.submitting).toBe(false)
  })

  it('离开页面使文件分页和后续批量提交失效', async () => {
    api.projects.mockResolvedValue(pageOf([project(1), project(2)]))
    const vm = await render()
    vm.form.scope = 'all'
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.files.mockReturnValue(pending.promise)
    const submitting = vm.onSubmit()
    await flushPromises()
    wrapper.unmount()
    pending.resolve(pageOf([codeFile(1)], 2, 1, 1))
    await submitting
    expect(api.files).toHaveBeenCalledTimes(1)
    expect(api.start).not.toHaveBeenCalled()
  })

  it('完成的批量结果不会因另一次单项目提交而冒充仍在处理', async () => {
    const vm = await render()
    vm.form.scope = 'all'
    await vm.onSubmit()
    vm.form.scope = 'whole'
    await chooseProject(vm)
    const pending = deferred<{ task_id: number; status: string }>()
    api.start.mockReturnValue(pending.promise)
    const submitting = vm.onSubmit()
    await flushPromises()
    expect(wrapper.get('.batch-results').text()).not.toContain('正在提交审查任务')
    expect(wrapper.get('.batch-results').text()).toContain('已处理 1 / 1')
    pending.resolve({ task_id: 9, status: 'pending' })
    await submitting
  })
})
