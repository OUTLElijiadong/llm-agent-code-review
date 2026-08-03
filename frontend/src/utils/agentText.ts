/**
 * ChatAgent 输出文本整理。
 *
 * 背景:markdown 渲染开启了 breaks:true,模型输出里的每个换行都会变成 <br>。
 * 模型可能输出连续空白行,旧版前端还可能把空行持久化成字面量 <wbr>。
 * 这里删除代码围栏之外的空白行和遗留哨兵,围栏内保持原样。
 */

interface CodeFence {
  marker: '`' | '~'
  length: number
}

function openingFence(line: string): CodeFence | null {
  const match = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
  if (!match) return null
  const run = match[1]
  if (run[0] === '`' && match[2].includes('`')) return null
  return { marker: run[0] as CodeFence['marker'], length: run.length }
}

function closesFence(line: string, fence: CodeFence): boolean {
  const match = /^ {0,3}(`{3,}|~{3,})[\t ]*$/.exec(line)
  return Boolean(
    match
    && match[1][0] === fence.marker
    && match[1].length >= fence.length,
  )
}

/** 只转换代码围栏之外的连续文本,围栏及其内部内容逐字保留。 */
export function transformOutsideCodeFences(
  value: string,
  transform: (segment: string) => string,
): string {
  const lines = value.replace(/\r\n?/g, '\n').split('\n')
  const segments: string[] = []
  let outside: string[] = []
  let fenced: string[] = []
  let fence: CodeFence | null = null

  const flushOutside = (): void => {
    if (!outside.length) return
    segments.push(transform(outside.join('\n')))
    outside = []
  }
  const flushFenced = (): void => {
    if (!fenced.length) return
    segments.push(fenced.join('\n'))
    fenced = []
  }

  for (const line of lines) {
    if (fence) {
      fenced.push(line)
      if (closesFence(line, fence)) {
        fence = null
        flushFenced()
      }
      continue
    }

    const opened = openingFence(line)
    if (opened) {
      flushOutside()
      fence = opened
      fenced.push(line)
      continue
    }
    outside.push(line)
  }
  flushFenced()
  flushOutside()
  return segments.join('\n')
}

/** 删除代码围栏之外的空白行和展示符号,围栏内文本保持原样。 */
export function normalizeAgentText(value: string): string {
  const lines = value.replace(/\r\n?/g, '\n').split('\n')
  const output: string[] = []
  let fence: CodeFence | null = null
  for (const rawLine of lines) {
    if (fence) {
      output.push(rawLine)
      if (closesFence(rawLine, fence)) fence = null
      continue
    }

    const opened = openingFence(rawLine)
    if (opened) {
      fence = opened
      output.push(rawLine)
      continue
    }

    const line = rawLine
      .replace(/<\/?wbr\b[^>]*>/gi, '')
      .replace(/^\s*•\s*/, '')
    if (line.trim() !== '') output.push(line)
  }
  return output.join('\n')
}
