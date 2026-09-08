import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const runtime = vi.hoisted(() => ({
  value: '', lineCount: 3,
  revealLineInCenter: vi.fn(), setPosition: vi.fn(), focus: vi.fn(), dispose: vi.fn(),
  deltaDecorations: vi.fn(), updateOptions: vi.fn(),
}))
vi.mock('monaco-editor', () => ({
  Range: class { constructor(public startLineNumber: number, public startColumn: number, public endLineNumber: number, public endColumn: number) {} },
  editor: {
    ScrollType: { Smooth: 0, Immediate: 1 }, defineTheme: vi.fn(), setTheme: vi.fn(), setModelLanguage: vi.fn(),
    create: (_element: HTMLElement, options: { value: string }) => {
      runtime.value = options.value
      return {
        ...runtime,
        getValue: () => runtime.value,
        setValue: (value: string) => { runtime.value = value },
        getModel: () => ({ getLineCount: () => runtime.lineCount }),
      }
    },
  },
}))

import CodeViewer from './CodeViewer.vue'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'

const wrappers: VueWrapper[] = []
function render() {
  const wrapper = mount(CodeViewer, { props: { code: '第一行\n第二行\n第三行', highlightLines: [2] } })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => { runtime.deltaDecorations.mockImplementation(() => ['decoration']) })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

describe('真实代码定位提示', () => {
  it('减少动态时即时滚动，立即定位和聚焦有效行，并显示明确行号', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    const wrapper = render()
    wrapper.vm.revealLine(2)
    await wrapper.vm.$nextTick()
    expect(runtime.revealLineInCenter).toHaveBeenCalledWith(2, 1)
    expect(runtime.setPosition).toHaveBeenCalledWith({ lineNumber: 2, column: 1 })
    expect(runtime.focus).toHaveBeenCalledOnce()
    expect(wrapper.get('[role="status"]').text()).toBe('已定位到第 2 行')
  })

  it('普通模式滚动到有效行，定位时保留原问题行装饰', async () => {
    const wrapper = render()
    wrapper.vm.revealLine(3)
    await wrapper.vm.$nextTick()
    expect(runtime.revealLineInCenter).toHaveBeenCalledWith(3, 0)
    expect(wrapper.get('[role="status"]').text()).toBe('已定位到第 3 行')
    const issue = runtime.deltaDecorations.mock.calls.find(([, items]) => items.some((item: { options: { className: string } }) => item.options.className === 'prism-line-issue'))
    expect(issue?.[1][0].range.startLineNumber).toBe(2)
  })

  it('无效行不滚动、不抢焦点，也不显示已定位的假提示', async () => {
    const wrapper = render()
    for (const line of [0, -1, 4, Number.NaN, 1.5]) wrapper.vm.revealLine(line)
    await wrapper.vm.$nextTick()
    expect(runtime.revealLineInCenter).not.toHaveBeenCalled()
    expect(runtime.focus).not.toHaveBeenCalled()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('文件内容更换立即清除旧定位提示，避免沿用上一文件行号', async () => {
    const wrapper = render()
    wrapper.vm.revealLine(2)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="status"]').exists()).toBe(true)
    await wrapper.setProps({ code: '新文件' })
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('卸载后迟到的定位不再操作已销毁 Monaco 实例', () => {
    const wrapper = render()
    const reveal = wrapper.getComponent(MonacoEditor).vm.revealLine
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    reveal(2)
    expect(runtime.dispose).toHaveBeenCalledOnce()
    expect(runtime.revealLineInCenter).not.toHaveBeenCalled()
  })
})
