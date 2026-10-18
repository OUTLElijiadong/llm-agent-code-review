import { expect, it } from 'vitest'
import { renderMarkdown, stripMarkdown } from './markdown'

it('升级消毒器后保留正常格式，恶意原生标记与脚本链接不可执行', () => {
  const html = renderMarkdown('**正常内容** [文档](https://example.com/docs)\n<script>alert(1)</script>\n[危险](javascript:alert(1))\n<img src=x onerror=alert(1)>')
  const root = document.createElement('div')
  root.innerHTML = html
  expect(root.querySelector('strong')?.textContent).toBe('正常内容')
  expect(root.querySelector('a')?.getAttribute('href')).toBe('https://example.com/docs')
  expect(root.querySelector('script, [onerror], [onload], a[href^="javascript:"]')).toBeNull()
})
it('继续移除无权限导航并保留代码示例，摘要剥离排版标记', () => {
  const html = renderMarkdown('[可见](/projects) [禁止](/admin)\n`[示例](/admin)`', { linkAllowed: href => href !== '/admin' })
  const root = document.createElement('div')
  root.innerHTML = html
  expect(root.querySelectorAll('a')).toHaveLength(1)
  expect(root.textContent).not.toContain('禁止')
  expect(root.querySelector('code')?.textContent).toBe('[示例](/admin)')
  expect(stripMarkdown('## 标题\n**说明**')).toBe('标题\n说明')
})

it('移除 AI 报告中未配对的强调星号，并保留合法星号语法', () => {
  const html = renderMarkdown('**3.- 位置：侦察阶段\n\n任务编号：sbx_3b317e05c0fa45128c20e31d\n\na*b 和 *.py\n\n`a*b`')
  const root = document.createElement('div')
  root.innerHTML = html

  expect(root.textContent).toContain('3.- 位置：侦察阶段')
  expect(root.textContent).not.toContain('**')
  expect(root.textContent).toContain('a*b 和 *.py')
  expect(root.querySelector('code')?.textContent).toBe('a*b')
})

it.each([
  '[危险](javascript:alert%281%29)',
  '[危险](data:text/html;base64,PHN2Zz4=)',
  '[危险](JaVaScRiPt:alert%281%29)',
  '<svg onload="alert(1)"></svg>',
  '<img src=x onerror="alert(1)">',
])('AI Markdown 的活动内容不能生成可执行节点或链接：%s', (payload) => {
  const root = document.createElement('div')
  root.innerHTML = renderMarkdown(`${payload}\n\n[安全引用](https://example.com/docs)`)
  expect(root.querySelectorAll('a')).toHaveLength(1)
  expect(root.querySelector('a')?.getAttribute('href')).toBe('https://example.com/docs')
  expect(root.querySelector('script, svg, iframe, img, [onload], [onerror]')).toBeNull()
})
