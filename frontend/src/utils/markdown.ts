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

/**
 * 渲染 markdown 并消毒,可安全用于 v-html
 * @param source - markdown 原文
 * @returns 消毒后的 HTML 字符串
 */
export function renderMarkdown(source: string): string {
  const html = md.render(source ?? '')
  return DOMPurify.sanitize(html)
}
