/**
 * 三端交互升级(第二批)回归:空态引导 + 缩放按钮。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

import EmptyState from '@/components/common/EmptyState.vue'

describe('三端交互升级第二批回归', () => {
  it('【空态引导】EmptyState 渲染行动按钮并可跳转(actionTo)', async () => {
    router.push.mockClear()
    const wrapper = mount(EmptyState, {
      props: {
        description: '还没有审查任务',
        actionText: '启动第一个审查',
        actionTo: '/reviews/start',
      },
    })
    expect(wrapper.text()).toContain('还没有审查任务')
    const btn = wrapper.find('.empty-action-btn')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('启动第一个审查')
    await btn.trigger('click')
    expect(router.push).toHaveBeenCalledWith('/reviews/start')
  })

  it('【空态引导】无 actionText 时不渲染按钮(筛选无结果场景)', () => {
    const wrapper = mount(EmptyState, {
      props: { description: '当前筛选条件下没有审查任务,试试放宽条件', actionText: '', actionTo: '' },
    })
    expect(wrapper.find('.empty-action-btn').exists()).toBe(false)
    expect(wrapper.text()).toContain('试试放宽条件')
  })

  it('【缩放】CodeViewer 暴露 zoomIn/zoomOut 且字号持久化在限定范围', async () => {
    vi.doMock('@/components/editor/MonacoEditor.vue', () => ({
      default: { name: 'MonacoEditor', render: () => null, methods: { updateOptions: () => undefined } },
    }))
    localStorage.removeItem('prism-code-font-size')
    const { default: CodeViewer } = await import('@/components/code/CodeViewer.vue')
    const wrapper = mount(CodeViewer, {
      props: { code: 'let x = 1', language: 'javascript', fileName: 'a.js' },
    })
    const exposed = wrapper.vm as unknown as { zoomIn: () => void; zoomOut: () => void }
    expect(typeof exposed.zoomIn).toBe('function')
    expect(typeof exposed.zoomOut).toBe('function')
    for (let i = 0; i < 30; i += 1) exposed.zoomIn()
    expect(Number(localStorage.getItem('prism-code-font-size'))).toBe(22)
    for (let i = 0; i < 40; i += 1) exposed.zoomOut()
    expect(Number(localStorage.getItem('prism-code-font-size'))).toBe(10)
    wrapper.unmount()
  })
})

describe('三端交互升级第三批回归', () => {
  it('【cron校验】isCronValid 五段格式判定(合法/非法边界)', async () => {
    const { isCronValid } = await import('@/utils/cronValidate')
    expect(isCronValid('0 3 * * *')).toBe(true)
    expect(isCronValid('*/5 * * * *')).toBe(true)
    expect(isCronValid('30 2 1,15 * 0')).toBe(true)
    expect(isCronValid('0 22 * * 1-5')).toBe(true)
    expect(isCronValid('* * *')).toBe(false)
    expect(isCronValid('* * * * * *')).toBe(false)
    expect(isCronValid('')).toBe(false)
    expect(isCronValid('   ')).toBe(false)
    expect(isCronValid('0 25 * * *')).toBe(false)
  })
})
