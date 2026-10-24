import { describe, expect, it } from 'vitest'

describe('小菱 Markdown 表格布局', () => {
  it('长表格保留单元格内容并在消息气泡内横向滚动（任意窗口宽度）', async () => {
    const source = (await import('./AgentChatDrawer.vue?raw')).default as string
    const tableRule = source.match(
      /\n\.msg-content\.markdown-body :deep\(table\)\s*\{([^}]+)\}/s,
    )?.[1]
    const cellRule = source.match(
      /\.msg-content\.markdown-body :deep\(th\),\s*\.msg-content\.markdown-body :deep\(td\)\s*\{([^}]+)\}/s,
    )?.[1]

    expect(tableRule).toBeTruthy()
    expect(tableRule).toMatch(/display:\s*block;/)
    expect(tableRule).toMatch(/overflow-x:\s*auto;/)
    expect(tableRule).toMatch(/max-width:\s*100%;/)
    expect(tableRule).toMatch(/overscroll-behavior-x:\s*contain;/)
    expect(tableRule).toMatch(/-webkit-overflow-scrolling:\s*touch;/)
    expect(cellRule).toMatch(/white-space:\s*nowrap;/)
  })
})
