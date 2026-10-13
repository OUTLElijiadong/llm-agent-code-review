import { defineComponent, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useAgentPageRefresh } from './useAgentPageRefresh'
import { setToken } from '@/utils/token'

const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.useFakeTimers(); setToken('current-account') })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })
function emit(page = '/forum') {
  window.dispatchEvent(new CustomEvent('prism:agent-page-invalidated', { detail: { page } }))
}
function render(refresh: () => unknown, allowed = ref(true)) {
  const wrapper = mount(defineComponent({ setup() {
    useAgentPageRefresh({ pages: ['/forum'], refresh, canRefresh: () => allowed.value })
    return () => null
  } }))
  wrappers.push(wrapper)
  return wrapper
}
it('refreshes only the affected business page and coalesces a burst', async () => {
  const refresh = vi.fn()
  render(refresh)
  emit('/support'); emit('/forum-other')
  vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).not.toHaveBeenCalled()
  emit('/forum/44'); emit('/forum?category=qa')
  vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).toHaveBeenCalledOnce()
})
it('retains a pending invalidation while the user edits and refreshes after the draft is closed', async () => {
  const refresh = vi.fn()
  const allowed = ref(false)
  render(refresh, allowed)
  emit(); vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).not.toHaveBeenCalled()
  allowed.value = true; await flushPromises()
  vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).toHaveBeenCalledOnce()
})
it('does not refresh a new account from the previous account pending receipt', async () => {
  const refresh = vi.fn()
  render(refresh)
  emit(); setToken('next-account')
  vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).not.toHaveBeenCalled()
})
it('removes its listener and timer on page leave, and catches read failures', async () => {
  const refresh = vi.fn().mockRejectedValue(new Error('transient read error'))
  const wrapper = render(refresh)
  emit(); vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).toHaveBeenCalledOnce()
  emit(); wrapper.unmount()
  vi.advanceTimersByTime(50); await flushPromises(); emit()
  vi.advanceTimersByTime(50); await flushPromises()
  expect(refresh).toHaveBeenCalledOnce()
})
