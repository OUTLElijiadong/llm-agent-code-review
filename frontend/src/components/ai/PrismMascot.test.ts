import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PrismMascot from './PrismMascot.vue'

describe('小菱圆润菱形精灵', () => {
  it.each([
    ['idle', '准备好了'], ['thinking', '正在思考'], ['working', '正在工作'],
    ['waiting', '等待你操作'], ['error', '遇到问题'],
  ] as const)('%s 状态具有可读语义且不依赖颜色', (status, text) => {
    const wrapper = mount(PrismMascot, { props: { status, decorative: false } })
    expect(wrapper.attributes('role')).toBe('img')
    expect(wrapper.attributes('aria-label')).toContain(text)
    expect(wrapper.attributes('data-state')).toBe(status)
    if (status === 'error' || status === 'waiting') expect(wrapper.find('.prismling-attention').exists()).toBe(true)
    if (status === 'thinking') expect(wrapper.find('.prismling-thought').exists()).toBe(true)
    wrapper.unmount()
  })

  it('旧 running 调用兼容工作态，按钮内装饰图不重复播报', () => {
    const wrapper = mount(PrismMascot, { props: { status: 'running', size: 22 } })
    expect(wrapper.attributes('data-state')).toBe('working')
    expect(wrapper.attributes('aria-hidden')).toBe('true')
    expect(wrapper.attributes('width')).toBe('22')
    wrapper.unmount()
  })

  it('同页多个头像不会共用渐变 ID', () => {
    const first = mount(PrismMascot)
    const second = mount(PrismMascot)
    const firstId = first.find('linearGradient').attributes('id')
    const secondId = second.find('linearGradient').attributes('id')
    expect(firstId).not.toBe(secondId)
    expect(first.find('.prismling-body').attributes('fill')).toBe(`url(#${firstId})`)
    expect(second.find('.prismling-body').attributes('fill')).toBe(`url(#${secondId})`)
    first.unmount()
    second.unmount()
  })
})
