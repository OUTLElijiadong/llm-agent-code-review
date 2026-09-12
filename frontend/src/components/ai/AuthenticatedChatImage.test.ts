import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AuthenticatedChatImage from './AuthenticatedChatImage.vue'

const api = vi.hoisted(() => ({ image: vi.fn(), store: { profile: { id: 5 } } }))
vi.mock('@/api/agentResponses', () => ({ fetchAgentResponseImage: api.image }))
vi.mock('@/stores/user', () => ({ useUserStore: () => api.store }))
const wrappers: ReturnType<typeof mount>[] = []

beforeEach(() => {
  api.store = reactive({ profile: { id: 5 } })
  api.image.mockReset().mockResolvedValue(new Blob(['image'], { type: 'image/png' }))
  vi.stubGlobal('URL', { createObjectURL: vi.fn().mockReturnValue('blob:fixture'), revokeObjectURL: vi.fn() })
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.unstubAllGlobals()
})
function render() {
  const wrapper = mount(AuthenticatedChatImage, { props: { assetId: 12, ownerId: 5 } })
  wrappers.push(wrapper)
  return wrapper
}

it('本人资产通过鉴权接口加载，卸载回收对象地址', async () => {
  const wrapper = render()
  await flushPromises()
  expect(api.image).toHaveBeenCalledWith(12)
  expect(wrapper.get('img').attributes('src')).toBe('blob:fixture')
  wrapper.unmount()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fixture')
})

it('404等读取失败有常驻重试，重试成功恢复图片', async () => {
  api.image.mockRejectedValueOnce(new Error('404'))
  const wrapper = render()
  await flushPromises()
  expect(wrapper.text()).toContain('图片暂时无法显示')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.find('img').exists()).toBe(true)
  expect(api.image).toHaveBeenCalledTimes(2)
})

it('换账号后丢弃旧未完成结果，不显示或重发旧账号图片', async () => {
  let resolve!: (blob: Blob) => void
  api.image.mockReturnValueOnce(new Promise<Blob>(done => { resolve = done }))
  const wrapper = render()
  api.store.profile.id = 6
  await flushPromises()
  resolve(new Blob(['old'], { type: 'image/png' }))
  await flushPromises()
  expect(wrapper.find('img').exists()).toBe(false)
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(api.image).toHaveBeenCalledTimes(1)
})
