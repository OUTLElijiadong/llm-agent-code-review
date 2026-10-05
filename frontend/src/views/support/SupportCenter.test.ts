import { defineComponent, onMounted, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { ElTabPane, ElTabs } from 'element-plus'
import { afterEach, expect, it, vi } from 'vitest'

const mountedFeedback = vi.hoisted(() => vi.fn())
vi.mock('./MaintenanceCenter.vue', () => ({ default: { template: '<section>支持工单</section>' } }))
vi.mock('./FeedbackCenter.vue', () => ({ default: defineComponent({
  props: { active: { type: Boolean, default: true } },
  setup() { const draft = ref(''); onMounted(mountedFeedback); return { draft } },
  template: '<section><label>反馈草稿<input v-model="draft"></label><span>{{ active ? "反馈激活" : "反馈暂停" }}</span></section>',
}) }))
import SupportCenter from './SupportCenter.vue'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); mountedFeedback.mockClear() })
async function render(query = '') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/support', component: SupportCenter }] })
  await router.push('/support' + query); await router.isReady()
  const wrapper = mount(SupportCenter, { attachTo: document.body,
    global: { plugins: [router], components: { ElTabs, ElTabPane }, stubs: { ElTag: { template: '<span><slot/></span>' } } } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}

it('真实tab切换控制反馈激活，草稿和组件实例保留，查询参数保持同步', async () => {
  const { wrapper, router } = await render('?ticket=42')
  expect(wrapper.text()).toContain('反馈暂停')
  expect(mountedFeedback).toHaveBeenCalledTimes(1)
  await wrapper.get('#tab-feedback').trigger('click'); await flushPromises()
  expect(router.currentRoute.value.query).toEqual({ ticket: '42', section: 'feedback' })
  expect(wrapper.text()).toContain('反馈激活')
  await wrapper.get('input').setValue('未提交的反馈草稿')
  await wrapper.get('#tab-maintenance').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('反馈暂停')
  expect(router.currentRoute.value.query.section).toBe('maintenance')
  await wrapper.get('#tab-feedback').trigger('click'); await flushPromises()
  expect((wrapper.get('input').element as HTMLInputElement).value).toBe('未提交的反馈草稿')
  expect(mountedFeedback).toHaveBeenCalledTimes(1)
})

it('浏览器查询参数切换会同步激活状态，未知section按既有规则回工单', async () => {
  const { wrapper, router } = await render('?section=feedback')
  expect(wrapper.get('#tab-feedback').attributes('aria-selected')).toBe('true')
  expect(wrapper.text()).toContain('反馈激活')
  await router.replace({ query: { section: 'invalid' } }); await flushPromises()
  expect(wrapper.get('#tab-maintenance').attributes('aria-selected')).toBe('true')
  expect(wrapper.text()).toContain('反馈暂停')
  await router.replace({ query: { section: 'feedback' } }); await flushPromises()
  expect(wrapper.text()).toContain('反馈激活')
  expect(mountedFeedback).toHaveBeenCalledTimes(1)
})
