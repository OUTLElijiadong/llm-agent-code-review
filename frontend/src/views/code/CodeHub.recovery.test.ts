import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import CodeHub from './CodeHub.vue'

const mocks = vi.hoisted(() => ({ getProjects: vi.fn(), push: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: mocks.getProjects }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))

beforeEach(() => {
  mocks.getProjects.mockReset()
  mocks.push.mockReset().mockResolvedValue(undefined)
})
function render() {
  return mount(CodeHub, { global: {
    directives: { loading: () => {} },
    stubs: {
      ElInput: { props: ['modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
      ElIcon: { template: '<span><slot/></span>' },
      ElTag: { template: '<span><slot/></span>' },
      ElAlert: { props: ['title', 'description'], template: '<section role="alert"><b>{{ title }}</b><p>{{ description }}</p><slot/></section>' },
      ElButton: { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot/></button>' },
    },
  } })
}
it('读取失败常驻反馈且不伪装无项目；点击重试恢复真实卡片和键盘进入', async () => {
  mocks.getProjects.mockRejectedValueOnce(new Error('network offline')).mockResolvedValueOnce({ items: [{
    id: 161, project_name: '混合来源项目', file_count: 16, active_file_count: 4, archive_file_count: 12,
  }] })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.text()).toContain('network offline')
  expect(wrapper.text()).not.toContain('还没有项目')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(mocks.getProjects).toHaveBeenCalledTimes(2)
  expect(wrapper.text()).not.toContain('network offline')
  expect(wrapper.text()).toContain('4 个代码库文件 · 12 个归档文件')
  const card = wrapper.get('article')
  expect(card.attributes('role')).toBe('button')
  expect(card.attributes('tabindex')).toBe('0')
  await card.trigger('keydown.enter')
  await card.trigger('keydown.space')
  expect(mocks.push.mock.calls).toEqual([['/code/161'], ['/code/161']])
  wrapper.unmount()
})
it('有项目但搜索无匹配时展示搜索状态，不提示创建项目', async () => {
  mocks.getProjects.mockResolvedValue({ items: [{ id: 2, project_name: '皮卡丘', file_count: 8, source_mode: 'audit_archive' }] })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.text()).toContain('8 个归档文件')
  await wrapper.get('input').setValue('不存在的名称')
  expect(wrapper.text()).toContain('没有匹配的项目')
  expect(wrapper.text()).not.toContain('还没有项目')
  await wrapper.get('input').setValue('')
  expect(wrapper.findAll('article')).toHaveLength(1)
  expect(mocks.getProjects).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})
it('成功读取空列表时才展示无项目状态', async () => {
  mocks.getProjects.mockResolvedValue({ items: [] })
  const wrapper = render()
  await flushPromises()
  expect(wrapper.text()).toContain('还没有项目')
  expect(wrapper.find('button').exists()).toBe(false)
  wrapper.unmount()
})
