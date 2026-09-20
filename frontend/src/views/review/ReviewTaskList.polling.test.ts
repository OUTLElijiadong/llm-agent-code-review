import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { deferred, pageOf, scanMountOptions } from './scanRegressionTestUtils'

const api = vi.hoisted(() => ({ tasks: vi.fn(), projects: vi.fn() }))
vi.mock('@/api/review', () => ({ getReviewTasks: api.tasks, deleteReviewTask: vi.fn(), cancelReviewTask: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: () => false }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
import ReviewTaskList from './ReviewTaskList.vue'

let wrapper: VueWrapper
const running = { id: 1, status: 'running' }
beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers()
  api.projects.mockResolvedValue(pageOf([]))
  api.tasks.mockResolvedValue(pageOf([running]))
})
afterEach(() => wrapper?.unmount())

async function render() {
  wrapper = mount(ReviewTaskList, scanMountOptions)
  await flushPromises()
  return wrapper.vm as any
}

describe('任务列表轮询恢复', () => {
  it.each(['2026-09-20T12:50:47', '2026-09-20T12:50:47Z', '2026-09-20T20:50:47+08:00'])('服务端时间 %s 转为本地时间，刷新时刻和日期筛选保持原义', async (createdAt) => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    try {
      vi.setSystemTime(new Date('2026-09-20T12:51:05Z'))
      expect(new Date().getTimezoneOffset()).toBe(-480)
      api.tasks.mockResolvedValue(pageOf([{ id: 179, status: 'success', create_time: createdAt }]))
      const vm = await render()
      expect(wrapper.get('.tc-line2').text()).toContain('2026-09-20 20:50')
      expect(wrapper.get('[role="status"]').text()).toContain('最近成功读取：2026-09-20 20:51:05')
      vm.dateRange = ['2026-09-20', '2026-09-21']
      await vm.loadData()
      expect(api.tasks).toHaveBeenLastCalledWith(expect.objectContaining({ start: '2026-09-20', end: '2026-09-21' }))
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('R8 轮询错误保留最后结果和错误提示，网络恢复后自动重试', async () => {
    api.tasks.mockResolvedValueOnce(pageOf([running])).mockRejectedValueOnce(new Error('连接中断')).mockResolvedValue(pageOf([{ id: 1, status: 'success' }]))
    const vm = await render()
    await vi.advanceTimersByTimeAsync(4000)
    await flushPromises()
    expect(vm.tasks).toEqual([running])
    expect(wrapper.text()).toContain('连接中断')
    expect(wrapper.text()).toContain('重试')
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(vm.tasks[0].status).toBe('success')
    expect(wrapper.text()).not.toContain('连接中断')
    const count = api.tasks.mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.tasks).toHaveBeenCalledTimes(count)
  })

  it('待排队任务同样持续轮询', async () => {
    api.tasks.mockResolvedValue(pageOf([{ id: 1, status: 'pending' }]))
    await render()
    await vi.advanceTimersByTimeAsync(4000)
    expect(api.tasks).toHaveBeenCalledTimes(2)
  })

  it('首次加载失败不伪报空数据并可以立即重试', async () => {
    api.tasks.mockRejectedValueOnce(new Error('服务暂不可用')).mockResolvedValue(pageOf([running]))
    await render()
    expect(wrapper.text()).toContain('服务暂不可用')
    const retry = wrapper.findAll('button').find(button => button.text().includes('立即重试'))
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(api.tasks).toHaveBeenCalledTimes(2)
  })

  it('卸载后在途响应不得重新启动轮询', async () => {
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.tasks.mockReturnValue(pending.promise)
    await render()
    wrapper.unmount()
    pending.resolve(pageOf([running]))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.tasks).toHaveBeenCalledTimes(1)
  })

  it('慢轮询不能覆盖用户刚切换的筛选结果', async () => {
    const vm = await render()
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.tasks.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(pageOf([{ id: 2, status: 'success' }]))
    await vi.advanceTimersByTimeAsync(4000)
    vm.filterStatus = 'success'
    await vm.loadData()
    pending.resolve(pageOf([running]))
    await flushPromises()
    expect(vm.tasks[0].id).toBe(2)
  })

  it('连续失败以4/8/16/30秒退避并封顶，成功后恢复4秒间隔', async () => {
    api.tasks.mockRejectedValue(new Error('连接失败'))
    const vm = await render()
    expect(api.tasks).toHaveBeenCalledTimes(1)
    for (const interval of [4000, 8000, 16000, 30000, 30000]) {
      const before = api.tasks.mock.calls.length
      await vi.advanceTimersByTimeAsync(interval - 1)
      expect(api.tasks).toHaveBeenCalledTimes(before)
      await vi.advanceTimersByTimeAsync(1)
      expect(api.tasks).toHaveBeenCalledTimes(before + 1)
    }
    api.tasks.mockResolvedValue(pageOf([running]))
    await vm.loadData()
    const before = api.tasks.mock.calls.length
    await vi.advanceTimersByTimeAsync(4000)
    expect(api.tasks).toHaveBeenCalledTimes(before + 1)
  })

  it('相同筛选的慢请求不可叠加定时轮询或手动刷新', async () => {
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.tasks.mockReturnValue(pending.promise)
    const vm = await render()
    await vm.loadData()
    await vi.advanceTimersByTimeAsync(120000)
    expect(api.tasks).toHaveBeenCalledTimes(1)
    pending.resolve(pageOf([running]))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(4000)
    expect(api.tasks).toHaveBeenCalledTimes(2)
  })

  it('缺少错误文字的拒绝仍显示错误并安排恢复，不伪报空列表', async () => {
    api.tasks.mockRejectedValueOnce({ message: '' }).mockResolvedValue(pageOf([running]))
    await render()
    expect(wrapper.text()).toContain('任务状态读取失败')
    await vi.advanceTimersByTimeAsync(4000)
    expect(api.tasks).toHaveBeenCalledTimes(2)
  })

  it('旧筛选的失败响应不能覆盖新筛选成功状态或重启重试', async () => {
    const vm = await render()
    const pending = deferred<ReturnType<typeof pageOf>>()
    api.tasks.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(pageOf([{ id: 2, status: 'success' }]))
    await vi.advanceTimersByTimeAsync(4000)
    vm.filterStatus = 'success'
    await vm.loadData()
    pending.reject(new Error('旧筛选错误'))
    await flushPromises()
    expect(vm.loadError).toBe('')
    const before = api.tasks.mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.tasks).toHaveBeenCalledTimes(before)
  })

  it('任务卡片只把success评分作为风险评级，失败历史100分和失败0分均显示未形成评分', async () => {
    const rows = [
      { status: 'failed', score: 100 }, { status: 'failed', score: 0 },
      { status: 'cancelled', score: 90 }, { status: 'pending', score: 0 },
      { status: 'running', score: 60 }, { status: 'success', score: 0 },
      { status: 'success', score: 100 },
    ].map((row, index) => ({ ...row, id: index + 1, project_name: '测试项目', review_type: 'standard',
      total_issues: 0, duration_ms: 0, create_time: '2026-09-05T00:00:00Z' }))
    api.tasks.mockResolvedValue(pageOf(rows))
    wrapper = mount(ReviewTaskList, {
      global: {
        ...scanMountOptions.global,
      },
    })
    await flushPromises()
    const renderedRows = wrapper.findAll('.task-card')
    expect(renderedRows).toHaveLength(7)
    for (const row of renderedRows.slice(0, 5)) {
      expect(row.get('.tc-score').text()).toBe('未形成评分')
      expect(row.find('.score-low, .score-medium, .score-high').exists()).toBe(false)
    }
    expect(renderedRows[5].get('.tc-score').text()).toBe('0')
    expect(renderedRows[5].find('.score-low').exists()).toBe(true)
    expect(renderedRows[6].get('.tc-score').text()).toBe('100')
    expect(renderedRows[6].find('.score-high').exists()).toBe(true)
  })
})


describe('审查卡片进度与可操作权限', () => {
  it('展示后端真实处理进度，并给详情保留可聚焦的按钮', async () => {
    api.tasks.mockResolvedValue(pageOf([{ id: 8, task_name: '真实进度', status: 'running', total_files: 5, processed_files: 2 }]))
    await render()
    expect(wrapper.get('.tc-progress').attributes('title')).toBe('2/5 文件')
    expect(wrapper.get('.tc-progress-fill').attributes('style')).toContain('width: 40%')
    expect(wrapper.get('button.tc-name').text()).toBe('真实进度')
  })
  it('缺少取消权限时不显示点击无反应的停止和删除按钮', async () => {
    await render()
    expect(wrapper.text()).not.toContain('停止')
    expect(wrapper.text()).not.toContain('删除')
  })
})
