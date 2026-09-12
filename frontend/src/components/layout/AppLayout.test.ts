import { describe, expect, it } from 'vitest'
import source from './AppLayout.vue?raw'

const styles = source.split('<style scoped lang="scss">')[1].split('</style>')[0]

describe('用户布局底部操作区避让小菱入口', () => {
  it('主滚动区为60px入口、24px底距和16px间隔保留空间，并包含设备安全区', () => {
    const main = styles.match(/\.app-layout-main\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(main).toContain('--assistant-action-clearance: calc(100px + env(safe-area-inset-bottom, 0px))')
    expect(main).toContain('padding-bottom: max(var(--layout-main-padding), var(--assistant-action-clearance))')
    expect(main).toContain('scroll-padding-bottom: var(--assistant-action-clearance)')
    expect(main).toContain('overflow-y: auto')
  })

  it.each([768, 420])('宽度不超过%dpx时只缩横向间距，不覆盖底部安全区', (width) => {
    const media = styles.split(`@media (max-width: ${width}px)`)[1]?.split('@media')[0] ?? ''
    const main = media.match(/\.app-layout-main\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(main).toContain('padding-inline:')
    expect(main).not.toMatch(/(?:^|;)\s*padding(?:-bottom)?\s*:/)
  })
})
