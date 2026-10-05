import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'

import AdminLayout from './AdminLayout.vue'
import source from './AdminLayout.vue?raw'

const styles = source.split('<style scoped lang="scss">')[1].split('</style>')[0]

describe('AdminLayout scrolling contract', () => {
  it('keeps sidebar and content in a fixed independent scroll shell', () => {
    expect(source).toContain('height: 100dvh')
    expect(source).toContain('overflow: hidden')
    expect(source).toContain('min-height: 0')
    expect(source).toContain('overflow-y: auto')
    expect(source).toContain('scrollTo')
    expect(source).toContain('overscroll-behavior: contain')
  })
})

describe('管理员底部操作区避让副驾入口', () => {
  it('主滚动区为60px入口、24px底距和16px间隔保留空间，并包含设备安全区', () => {
    const content = styles.match(/\.admin-content\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(content).toContain('--assistant-action-clearance: calc(100px + env(safe-area-inset-bottom, 0px))')
    expect(content).toContain('padding-bottom: max(36px, var(--assistant-action-clearance))')
    expect(content).toContain('scroll-padding-bottom: var(--assistant-action-clearance)')
    expect(content).toContain('overflow-y: auto')
  })

  it.each([920, 480])('宽度不超过%dpx时保持底部安全区，只调整顶边与横向间距', (width) => {
    const media = styles.split(`@media (max-width: ${width}px)`)[1]?.split('@media')[0] ?? ''
    const content = media.match(/\.admin-content\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(content).toContain('padding-top:')
    expect(content).toContain('padding-inline:')
    expect(content).not.toMatch(/(?:^|;)\s*padding(?:-bottom)?\s*:/)
  })
})

describe('管理员手机端小菱入口', () => {
  it('入口有独立头部槽位，避免固定浮窗盖住运行卡片', () => {
    expect(source).toContain('id="admin-copilot-trigger-slot"')
    expect(styles).toContain('@media (max-width: 520px)')
    expect(styles).toMatch(/\.admin-copilot-trigger-slot\s*\{[^}]*min-height:\s*44px;/s)
    const mobile = styles.split('@media (max-width: 520px)')[1]?.split('@media')[0] ?? ''
    expect(mobile).toMatch(/\.admin-copilot-trigger-slot\s*\{[^}]*display:\s*grid;/s)
  })
})

it('安全中心在页头预留小菱入口，离开该页面恢复其他管理页布局', async () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/admin/:pathMatch(.*)*', component: { template: '<main />' } }],
  })
  await router.push('/admin/security-center')
  const wrapper = mount(AdminLayout, {
    global: {
      plugins: [createPinia(), router],
      stubs: {
        ProactivePageGuide: true,
        UserAvatar: true,
        'el-dropdown': { template: '<div><slot /></div>' },
        'el-dropdown-menu': true,
        'el-dropdown-item': true,
        'el-icon': { template: '<span><slot /></span>' },
      },
    },
  })
  const content = wrapper.get<HTMLElement>('.admin-content').element
  content.scrollTo = vi.fn()
  try {
    const slot = wrapper.get('#admin-copilot-trigger-slot')
    expect(slot.element.closest('.admin-header')).not.toBeNull()
    expect(slot.classes()).toContain('is-page-header-trigger')

    await router.push('/admin/security-center?tab=events')
    await flushPromises()
    expect(slot.classes()).toContain('is-page-header-trigger')

    await router.push('/admin/operations')
    await flushPromises()
    expect(slot.classes()).not.toContain('is-page-header-trigger')
  } finally {
    wrapper.unmount()
  }
})
