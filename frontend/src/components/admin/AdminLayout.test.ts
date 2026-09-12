import { describe, expect, it } from 'vitest'

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
