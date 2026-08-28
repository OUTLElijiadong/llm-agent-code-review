/**
 * Markdown 渲染工具
 * 统一全站 markdown → HTML 的出口,渲染结果一律经 DOMPurify 消毒,
 * 防止 AI 产出 / 用户输入 / 论坛帖子中的恶意标记造成存储型 XSS。
 */
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const md = new MarkdownIt({
  html: false, // 不解析原生 HTML,源头先挡一道
  linkify: true,
  breaks: true,
})

export interface MarkdownRenderOptions {
  /** 返回 false 时移除整个站内链接及其文字,避免渲染不可用入口。 */
  linkAllowed?: (href: string) => boolean
}

/**
 * 在 Markdown token 层过滤链接,不使用正则改写源码,避免误伤代码围栏和链接文本。
 */
function renderWithLinkFilter(source: string, linkAllowed: (href: string) => boolean): string {
  const env: Record<string, unknown> = {}
  const tokens = md.parse(source, env)
  const filterTokens = (items: typeof tokens): typeof tokens => {
    const filtered = [] as typeof tokens
    let hiddenLinkDepth = 0

    for (const token of items) {
      if (hiddenLinkDepth > 0) {
        if (token.type === 'link_open') hiddenLinkDepth += 1
        if (token.type === 'link_close') hiddenLinkDepth -= 1
        continue
      }

      if (token.type === 'link_open' && !linkAllowed(token.attrGet('href') ?? '')) {
        hiddenLinkDepth = 1
        continue
      }

      if (token.children?.length) {
        token.children = filterTokens(token.children)
      }
      filtered.push(token)
    }

    return filtered
  }

  return md.renderer.render(filterTokens(tokens), md.options, env)
}

/**
 * 渲染 markdown 并消毒,可安全用于 v-html
 * @param source - markdown 原文
 * @returns 消毒后的 HTML 字符串
 */
export function renderMarkdown(source: string, options?: MarkdownRenderOptions): string {
  const normalizedSource = source ?? ''
  const html = options?.linkAllowed
    ? renderWithLinkFilter(normalizedSource, options.linkAllowed)
    : md.render(normalizedSource)
  return DOMPurify.sanitize(html)
}

/**
 * 提取 markdown 的纯文本(剥离 ** ## ` > - 等标记)。
 * 用于报告摘要等需要纯文本、而非 HTML 渲染的场景,
 * 避免 AI 产出的 markdown 符号直接裸露在界面上。
 * @param source - markdown 原文
 * @returns 纯文本
 */
export function stripMarkdown(source: string): string {
  const html = md.render(source ?? '')
  const text = DOMPurify.sanitize(html, { ALLOWED_TAGS: [], KEEP_CONTENT: true })
  return text.replace(/\s+\n/g, '\n').trim()
}
