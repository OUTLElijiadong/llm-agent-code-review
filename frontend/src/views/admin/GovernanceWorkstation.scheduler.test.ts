import { flushPromises, shallowMount } from '@vue/test-utils'
import { h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const governanceApi = vi.hoisted(() => ({
  listJobs: vi.fn(),
  updateJob: vi.fn(),
}))
const messages = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))
const confirm = vi.hoisted(() => vi.fn())
const router = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('@/api/adminGovernance', () => governanceApi)
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({ isSuperAdmin: () => true }),
}))
vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm } }))

import GovernanceWorkstation from './GovernanceWorkstation.vue'
import { isScheduleValid } from '@/utils/cronValidate'

const job = {
  id: 1,
  job_code: 'daily_agent_reflection',
  job_type: 'reflection',
  agent_code: 'reflection',
  schedule: 'daily@03:00',
  status: 'enabled',
}

const GenericStub = { template: '<div><slot /></div>' }
const ButtonStub = {
  inheritAttrs: false,
  props: ['disabled', 'loading'],
  template: '<button v-bind="$attrs" :disabled="disabled" :aria-busy="loading"><slot /></button>',
}
const TableStub = { template: '<div><slot /></div>' }
const TableColumnStub = {
  setup(_: unknown, context: { slots: { default?: (props: { row: typeof job }) => unknown } }) {
    return () => h('div', context.slots.default?.({ row: job }) as any)
  },
}

function mountJobs() {
  return shallowMount(GovernanceWorkstation, {
    props: { mode: 'jobs' },
    global: {
      stubs: {
        'el-alert': GenericStub,
        'el-button': ButtonStub,
        'el-input': GenericStub,
        'el-select': GenericStub,
        'el-option': GenericStub,
        'el-table': TableStub,
        'el-table-column': TableColumnStub,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  confirm.mockResolvedValue(true)
  governanceApi.listJobs.mockResolvedValue([{ ...job }])
  governanceApi.updateJob.mockResolvedValue({ ...job })
})

describe('GovernanceWorkstation scheduler compatibility', () => {
  it.each(['daily@03:00', 'hourly@*:23', 'hourly@23', 'interval@5m', 'interval@30s'])('accepts backend schedule %s', (schedule) => {
    expect(isScheduleValid(schedule)).toBe(true)
  })

  it('keeps strict five-field cron validation while rejecting malformed legacy values', () => {
    expect(isScheduleValid('*/5 * * * *')).toBe(true)
    expect(isScheduleValid('0 25 * * *')).toBe(false)
    expect(isScheduleValid('daily@25:00')).toBe(false)
    expect(isScheduleValid('hourly@60')).toBe(false)
    expect(isScheduleValid('interval@0m')).toBe(false)
    expect(isScheduleValid('interval@86401s')).toBe(false)
    expect(isScheduleValid('interval@5')).toBe(false)
  })

  it.each(['daily@02:00', 'hourly@*:17', 'hourly@17', 'interval@7m', 'interval@45s'])('submits existing %s without cron false positive', async (schedule) => {
    const wrapper = mountJobs()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.jobEdit[job.id].schedule = schedule

    await vm.onSaveJob(job)

    expect(governanceApi.updateJob).toHaveBeenCalledWith(job.id, { schedule, status: 'enabled' })
    expect(messages.success).toHaveBeenCalledWith('任务配置已保存')
    wrapper.unmount()
  })

  it('rejects invalid schedule before submitting', async () => {
    const wrapper = mountJobs()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.jobEdit[job.id].schedule = 'hourly@60'

    await vm.onSaveJob(job)

    expect(governanceApi.updateJob).not.toHaveBeenCalled()
    expect(messages.warning).toHaveBeenCalledWith('调度计划格式不正确，请使用已有格式或五段 cron')
    wrapper.unmount()
  })

  it('shows saving state and disables the row save button until request settles', async () => {
    let resolveUpdate!: (value: typeof job) => void
    governanceApi.updateJob.mockImplementationOnce(() => new Promise((resolve) => { resolveUpdate = resolve }))
    const wrapper = mountJobs()
    await flushPromises()
    const vm = wrapper.vm as any
    const saving = vm.onSaveJob(job)
    await flushPromises()
    await nextTick()

    expect(vm.jobSaving[job.id]).toBe(true)
    expect(wrapper.find(`[data-testid="save-job-${job.id}"]`).attributes('disabled')).toBeDefined()
    expect(wrapper.find(`[data-testid="save-job-${job.id}"]`).attributes('aria-busy')).toBe('true')

    resolveUpdate({ ...job })
    await saving
    expect(vm.jobSaving[job.id]).toBe(false)
    expect(wrapper.find(`[data-testid="save-job-${job.id}"]`).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows the backend error, preserves edits, and clears saving state', async () => {
    governanceApi.updateJob.mockRejectedValueOnce({ message: '调度器同步失败，请稍后重试' })
    const wrapper = mountJobs()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.jobEdit[job.id].schedule = 'interval@9m'

    await vm.onSaveJob(job)

    expect(vm.jobEdit[job.id].schedule).toBe('interval@9m')
    expect(vm.jobSaving[job.id]).toBe(false)
    expect(vm.jobSaveErrors[job.id]).toBe('调度器同步失败，请稍后重试')
    expect(messages.error).toHaveBeenCalledWith('调度器同步失败，请稍后重试')
    wrapper.unmount()
  })
})
