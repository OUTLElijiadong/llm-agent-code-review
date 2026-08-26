import { readdirSync, readFileSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

function vueFiles(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name)
    if (entry.isDirectory()) return vueFiles(path)
    return extname(entry.name) === '.vue' ? [path] : []
  })
}

describe('Element Plus 组件契约', () => {
  it('所有 radio 选项使用 value，不再触发 label 弃用警告', () => {
    const sourceRoot = resolve(process.cwd(), 'src')
    const violations = vueFiles(sourceRoot).flatMap((path) => {
      const source = readFileSync(path, 'utf8')
      const tags = source.match(/<el-radio(?:-button)?\b[^>]*>/gs) ?? []
      return tags
        .filter((tag) => /\s(?::|v-bind:)?label\s*=/.test(tag))
        .map((tag) => `${path}: ${tag.replace(/\s+/g, ' ')}`)
    })

    expect(violations).toEqual([])
  })
})
