import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import UserAvatar from './UserAvatar.vue'
import { invalidateAvatarCache } from '@/constants/avatars'
const { fetchAvatarBlob } = vi.hoisted(() => ({ fetchAvatarBlob: vi.fn() }))
vi.mock('@/api/avatar', () => ({ fetchAvatarBlob }))
beforeEach(() => {
  URL.createObjectURL = vi.fn().mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second')
  URL.revokeObjectURL = vi.fn()
  invalidateAvatarCache()
  fetchAvatarBlob.mockResolvedValue(new Blob(['image']))
})
it('重新上传同账号头像会刷新当前组件', async () => {
  const wrapper = mount(UserAvatar, { props: { avatar: 'upload', userId: 1 } })
  await flushPromises()
  expect(wrapper.get('img').attributes('src')).toBe('blob:first')
  invalidateAvatarCache(1)
  await flushPromises()
  expect(wrapper.get('img').attributes('src')).toBe('blob:second')
  wrapper.unmount()
})
it('旧账号延迟请求不能覆盖新账号头像', async () => {
  let resolveOld!: (blob: Blob) => void
  fetchAvatarBlob.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
  const wrapper = mount(UserAvatar, { props: { avatar: 'upload', userId: 1 } })
  await wrapper.setProps({ avatar: 'builtin:cat', userId: 2 })
  resolveOld(new Blob(['old']))
  await flushPromises()
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  wrapper.unmount()
})
