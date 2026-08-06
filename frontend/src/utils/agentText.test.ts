import { describe, expect, it } from 'vitest'

import { normalizeAgentText } from './agentText'

describe('normalizeAgentText', () => {
  it('removes blank lines, legacy wbr markers, and visible bullet characters outside code', () => {
    expect(normalizeAgentText('第一段\n\n\n<wbr>\n    •第二段')).toBe('第一段\n第二段')
  })

  it('preserves blank lines and marker-like text inside fenced code', () => {
    const source = '说明\n\n```html\n<wbr>\n\n<div>内容</div>\n```\n\n完成'

    expect(normalizeAgentText(source)).toBe(
      '说明\n```html\n<wbr>\n\n<div>内容</div>\n```\n完成',
    )
  })

  it('uses the opening fence length when detecting the closing fence', () => {
    const source = '说明\n````md\n代码前\n```\n\n代码后\n````\n完成'

    expect(normalizeAgentText(source)).toBe(source)
  })
})
