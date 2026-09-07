// 从 Vue 模板 AST 建立交互控件清单。清单不等于浏览器点击证据。
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { parse as parseTemplate } from '@vue/compiler-dom'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = process.argv[2]
if (!output) throw new Error('必须指定输出 JSON 路径')
const files = []
function collect(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name)
    if (entry.isDirectory()) collect(file)
    else if (entry.name.endsWith('.vue')) files.push(file)
  }
}
collect(path.join(root, 'src'))

// 直接读取 AST 文本，避免表达式属性中的 > 被误当作 HTML 标签终点。
function readableText(node) {
  if (node.type === 2) return node.content
  if (node.type === 5) return `{{ ${node.content.content} }}`
  return (node.children || []).map(readableText).join(' ')
}

const actions = []
const sources = []
for (const file of files.sort()) {
  const source = fs.readFileSync(file, 'utf8')
  sources.push({ file: path.relative(root, file), sha256: createHash('sha256').update(source).digest('hex') })
  const { descriptor, errors } = parseSfc(source, { filename: file })
  if (errors.length) throw new Error(`${file}: ${errors.join('; ')}`)
  if (!descriptor.template) continue
  const ast = parseTemplate(descriptor.template.content)
  function walk(node, inheritedConditions = []) {
    const conditions = [...inheritedConditions]
    for (const prop of node.props || []) {
      if (prop.type === 7 && ['if', 'else-if', 'else', 'for', 'show'].includes(prop.name)) {
        conditions.push(`${prop.name}: ${prop.exp?.content || ''}`)
      }
    }
    if (node.type === 1) {
      const events = (node.props || []).filter((prop) => prop.type === 7 && prop.name === 'on')
        .map((prop) => ({ event: prop.arg?.content || '', handler: prop.exp?.content || '' }))
      const isButton = ['button', 'el-button', 'el-dropdown-item', 'a', 'router-link', 'RouterLink'].includes(node.tag)
      if (isButton || events.length) {
        const sourceLine = descriptor.template.loc.start.line + node.loc.start.line - 1
        const readable = readableText(node).replace(/\s+/g, ' ').trim().slice(0, 180)
        const attributes = (node.props || []).filter((prop) => prop.type === 6)
          .map((prop) => [prop.name, prop.value?.content || ''])
        const bindings = (node.props || []).filter((prop) => prop.type === 7 && prop.name === 'bind')
          .map((prop) => [prop.arg?.content || '', prop.exp?.content || ''])
        actions.push({
          id: `${path.relative(root, file)}:${sourceLine}:${actions.length + 1}`,
          file: path.relative(root, file), line: sourceLine, tag: node.tag, button: isButton,
          label: Object.fromEntries(attributes)['aria-label'] || readable, events,
          attributes: Object.fromEntries(attributes), bindings: Object.fromEntries(bindings), conditions,
          local_browser_status: '未测', production_browser_status: '未测',
        })
      }
    }
    for (const child of node.children || []) walk(child, conditions)
  }
  walk(ast)
}
fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true })
fs.writeFileSync(output, JSON.stringify({
  generated_at: new Date().toISOString(), description: '源码交互清单，不能当成逐控件实际点击通过证据',
  vue_files: files.length, action_count: actions.length, button_count: actions.filter((item) => item.button).length,
  sources, actions,
}, null, 2) + '\n')
console.log(JSON.stringify({ files: files.length, actions: actions.length, buttons: actions.filter((item) => item.button).length }))
