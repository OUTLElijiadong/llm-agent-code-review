import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const permissionState = vi.hoisted(() => ({ allowed: false }))
const projectApi = vi.hoisted(() => ({
  getProjects: vi.fn(),
  deleteProject: vi.fn(),
  createProject: vi.fn(),
  updateProject: vi.fn(),
  importRemoteProject: vi.fn(),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    hasPermission: (code: string) => code === 'project:import' && permissionState.allowed,
  }),
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/api/project', () => projectApi)
vi.mock('@/api/codeFile', () => ({ uploadFolder: vi.fn() }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn() },
}))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

import ProjectList from './ProjectList.vue'

const ButtonStub = {
  inheritAttrs: false,
  template: '<button v-bind="$attrs"><slot /></button>',
}
const GenericStub = { template: '<div><slot /></div>' }

function mountProjectList() {
  return shallowMount(ProjectList, {
    global: {
      stubs: {
        'el-button': ButtonStub,
        'el-input': GenericStub,
        'el-option': GenericStub,
        'el-select': GenericStub,
        'el-pagination': GenericStub,
        'el-form-item': GenericStub,
        'el-form': GenericStub,
        'el-alert': GenericStub,
        'el-dialog': GenericStub,
        'el-tooltip': GenericStub,
        'el-checkbox': GenericStub,
        ProjectForm: true,
        EmptyState: true,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  permissionState.allowed = false
  projectApi.getProjects.mockResolvedValue({ items: [], total: 0 })
})

describe('ProjectList remote import permission', () => {
  it('不向缺少 project:import 权限的只读角色显示远程导入', async () => {
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="remote-import-button"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('向具备 project:import 权限的普通用户显示远程导入', async () => {
    permissionState.allowed = true
    const wrapper = mountProjectList()
    await flushPromises()

    expect(wrapper.find('[data-testid="remote-import-button"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ProjectList 视图切换', () => {
  it('通过 aria-pressed 暴露当前视图并在点击后同步更新', async () => {
    const wrapper = mountProjectList()
    await flushPromises()

    const [tableButton, cardButton] = wrapper.findAll('.view-btn')
    expect(tableButton.attributes('aria-pressed')).toBe('true')
    expect(cardButton.attributes('aria-pressed')).toBe('false')
    expect(wrapper.find('tbody td[colspan]').attributes('colspan')).toBe('9')

    await cardButton.trigger('click')

    expect(tableButton.attributes('aria-pressed')).toBe('false')
    expect(cardButton.attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('.card-grid').isVisible()).toBe(true)
    expect(wrapper.find('.table-card').isVisible()).toBe(false)
    wrapper.unmount()
  })
})
