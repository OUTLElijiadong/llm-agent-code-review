import { describe, expect, it } from 'vitest'

describe('小菱窄屏 Markdown 表格布局', () => {
  it('长表格保留单元格内容并在消息气泡内横向滚动', async () => {
    const source = (await import('./AgentChatDrawer.vue?raw')).default as string
    const tableRule = source.match(
      /@media\s*\(max-width:\s*640px\)\s*\{\s*\.msg-content\.markdown-body :deep\(table\)\s*\{([^}]+)\}\s*\}/s,
    )?.[1]

    expect(tableRule).toBeTruthy()
    expect(tableRule).toMatch(/display:\s*block;/)
    expect(tableRule).toMatch(/overflow-x:\s*auto;/)
    expect(tableRule).toMatch(/white-space:\s*nowrap;/)
    expect(tableRule).toMatch(/overscroll-behavior-x:\s*contain;/)
    expect(tableRule).toMatch(/-webkit-overflow-scrolling:\s*touch;/)
  })
})
