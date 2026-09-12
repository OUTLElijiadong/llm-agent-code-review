import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
const mocks = vi.hoisted(() => ({ init: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }))
vi.mock('echarts/core', () => ({ use: vi.fn(), registerTheme: vi.fn(), init: mocks.init }))
import BaseChart from './BaseChart.vue'
let visible = true
let observed: ResizeObserverCallback | undefined
let disconnected = vi.fn<() => void>()
beforeEach(() => {
  visible = true
  observed = undefined
  disconnected = vi.fn()
  mocks.init.mockReset().mockReturnValue({ setOption: mocks.setOption, resize: mocks.resize, dispose: mocks.dispose })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => visible ? 320 : 0)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => 220)
  window.ResizeObserver = class {
    constructor(callback: ResizeObserverCallback) { observed = callback }
    observe() {}
    unobserve() {}
    disconnect() { disconnected() }
  }
})
afterEach(() => vi.unstubAllGlobals())
function notifyResize() { observed?.([], {} as ResizeObserver) }
describe('图表隐藏恢复与异常兜底', () => {
  it('折叠区域初载不在零宽容器初始化，展开后实际生成图表', async () => {
    visible = false
    const wrapper = mount(BaseChart, { props: { option: { series: [] } } })
    expect(mocks.init).not.toHaveBeenCalled()
    visible = true
    notifyResize()
    await nextTick()
    expect(mocks.init).toHaveBeenCalledTimes(1)
    expect(mocks.setOption).toHaveBeenCalledWith({ series: [] })
    wrapper.unmount()
    expect(disconnected).toHaveBeenCalled()
  })
  it('侧栏折叠等容器变化无需 window resize 就刷新画布', () => {
    const wrapper = mount(BaseChart, { props: { option: {} } })
    notifyResize()
    expect(mocks.resize).toHaveBeenCalled()
    wrapper.unmount()
  })
  it('渲染失败提示可重试，重试成功恢复图表', async () => {
    mocks.init.mockImplementationOnce(() => { throw new Error('canvas unavailable') })
    const wrapper = mount(BaseChart, { props: { option: {} } })
    await nextTick()
    expect(wrapper.get('[role="alert"]').text()).toContain('图表暂时无法显示')
    await wrapper.get('button').trigger('click')
    expect(mocks.init).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
