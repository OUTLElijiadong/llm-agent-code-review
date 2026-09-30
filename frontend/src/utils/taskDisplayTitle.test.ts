import { describe, expect, it } from 'vitest'
import { taskDisplayTitle } from './taskDisplayTitle'

describe('审查任务显示名称', () => {
  it.each([
    ['项目167 完整代码审查（review_type=full）', '项目167 完整代码审查'],
    ['项目167 完整代码审查 (review_type=full)', '项目167 完整代码审查'],
  ])('移除自动附加的内部审查类型后缀 %s', (input, expected) => {
    expect(taskDisplayTitle(input)).toBe(expected)
  })

  it('保留标题中的其他括号文本和未知类型', () => {
    expect(taskDisplayTitle('业务审查（review_type=experimental）')).toBe('业务审查（review_type=experimental）')
    expect(taskDisplayTitle('项目167 (full)')).toBe('项目167 (full)')
  })

  it('为空名称返回调用方提供的回退值', () => {
    expect(taskDisplayTitle('  ', '审查 #7')).toBe('审查 #7')
  })
})
