import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useLiveAgentTeamPolling } from './useLiveAgentTeamPolling'

afterEach(() => vi.useRealTimers())
describe('SSE期间团队同步', () => {
  it('一次同步失败后继续拉取且无未处理拒绝', async () => {
    vi.useFakeTimers()
    const refresh = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValue(undefined)
    const wrapper = mount(defineComponent({ setup() {
      useLiveAgentTeamPolling({ active: () => true, scope: () => 'user:a', refresh })
      return () => null
    } }))
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(refresh).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
  it('开始立即同步，持续同步，停止或卸载后停止', async () => {
    vi.useFakeTimers()
    const active = ref(true)
    const scope = ref('user:a')
    const refresh = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(defineComponent({ setup() {
      useLiveAgentTeamPolling({ active: () => active.value, scope: () => scope.value, refresh })
      return () => null
    } }))
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(refresh).toHaveBeenCalledTimes(2)
    active.value = false
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    expect(refresh).toHaveBeenCalledTimes(2)
    active.value = true
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(3)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(3000)
    expect(refresh).toHaveBeenCalledTimes(3)
  })
  it('会话切换后旧请求不续轮询', async () => {
    vi.useFakeTimers()
    const scope = ref('user:a')
    let finish: () => void = () => {}
    const refresh = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve })).mockResolvedValue(undefined)
    const wrapper = mount(defineComponent({ setup() {
      useLiveAgentTeamPolling({ active: () => true, scope: () => scope.value, refresh })
      return () => null
    } }))
    await vi.advanceTimersByTimeAsync(0)
    scope.value = 'user:b'
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    expect(refresh).toHaveBeenCalledTimes(2)
    finish()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(refresh).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })
})
