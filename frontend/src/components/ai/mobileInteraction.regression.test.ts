import { describe, expect, it } from 'vitest'

describe('小菱与团队浮窗触控布局回归', () => {
  it('管理副驾驶头部使用 Flex，横屏及触控按钮不会挤成两行', async () => {
    const source = (await import('@/components/admin/AdminCopilot.vue?raw')).default as string

    expect(source).toMatch(/\.copilot-header\s*\{[^}]*display:\s*flex;/s)
    expect(source).toMatch(/\.copilot-identity\s*\{[^}]*flex:\s*1;/s)
    expect(source).toContain('@media (max-height: 520px) and (min-width: 521px)')
    expect(source).toMatch(/\.message-copy-btn\s*\{[^}]*width:\s*40px;[^}]*height:\s*40px;[^}]*opacity:\s*1;/s)
  })

  it('用户小菱在低高度横屏时隐藏吉祥物并让快捷问题回到文档流', async () => {
    const source = (await import('./AgentChatDrawer.vue?raw')).default as string

    expect(source).toContain('@media (max-height: 520px) and (min-width: 521px)')
    expect(source).toMatch(/@media \(max-height: 520px\)[\s\S]*?\.mascot-hero\s*\{\s*display:\s*none;/)
    expect(source).toMatch(/@media \(max-height: 520px\)[\s\S]*?\.quick-questions\s*\{[^}]*position:\s*static;/s)
  })

  it('团队窗口使用动态视口高度并允许窄屏进度换行', async () => {
    const source = (await import('./AgentTeamWindow.vue?raw')).default as string

    expect(source).toContain('max-height: min(600px, calc(100dvh - 48px));')
    expect(source).toContain('@media (max-height: 520px) and (min-width: 521px)')
    expect(source).toMatch(/\.team-window-task-details\s*>\s*summary\s*\{[^}]*min-height:\s*40px;/s)
    expect(source).toMatch(/\.team-window-progress-top\s*\{[^}]*flex-wrap:\s*wrap;/s)
  })

  it('成员追问按钮提供至少 40px 操作热区', async () => {
    const source = (await import('./AgentMemberWorkCard.vue?raw')).default as string
    expect(source).toMatch(/\.member-work-ask\s*\{[^}]*min-height:\s*40px;/s)
  })
})
