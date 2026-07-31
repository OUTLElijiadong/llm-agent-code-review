import { expect, it } from 'vitest'

import type { ClarifyQuestion } from '@/types/agentEvent'
import {
  CUSTOM_PROJECT_OPTION_VALUE,
  mergeProjectClarifyOptions,
  prepareClarifyAnswers,
  resolveProjectClarifyOptions,
} from '@/utils/clarifyProjectOptions'

const projectQuestion: ClarifyQuestion = {
  key: 'project_id',
  label: '请选择项目',
  type: 'select_project',
  required: true,
}

/** 验证推荐候选不会覆盖完整项目列表，且其他入口固定在末尾。 */
it('merges recommended and searched projects with a custom option', function testProjectOptionMerge(): void {
  const options = mergeProjectClarifyOptions(
    [{ value: 3, label: '#3 皮卡丘商城' }],
    [
      { value: 3, label: '#3 重复项目' },
      { value: 5, label: '#5 订单中心' },
    ],
  )

  expect(options).toEqual([
    { value: 3, label: '#3 皮卡丘商城' },
    { value: 5, label: '#5 订单中心' },
    { value: CUSTOM_PROJECT_OPTION_VALUE, label: '其他（自定义输入）' },
  ])
})

/** 验证输入关键词后只显示远程匹配结果，不混入无关初始推荐。 */
it('narrows project options to fuzzy search results', function testProjectSearchNarrowing(): void {
  const options = resolveProjectClarifyOptions(
    [{ value: 27, label: '#27 AC2-E2E' }],
    [{ value: 27, label: '#27 AC2-E2E' }, { value: 23, label: '#23 皮卡丘漏洞平台' }],
    [{ value: 23, label: '#23 皮卡丘漏洞平台' }, { value: 17, label: '#17 皮卡丘漏洞靶场' }],
    '皮卡丘',
  )

  expect(options).toEqual([
    { value: 23, label: '#23 皮卡丘漏洞平台' },
    { value: 17, label: '#17 皮卡丘漏洞靶场' },
    { value: CUSTOM_PROJECT_OPTION_VALUE, label: '其他（自定义输入）' },
  ])
})

/** 验证选择已知项目时直接保留项目 ID。 */
it('keeps a selected project id in clarify answers', function testKnownProjectAnswer(): void {
  const result = prepareClarifyAnswers(
    [projectQuestion],
    { project_id: 5 },
    {},
  )

  expect(result.missing).toBeUndefined()
  expect(result.answers).toEqual({ project_id: 5 })
})

/** 验证自定义数字或带井号数字会转换为项目 ID。 */
it('converts a custom numeric value to project id', function testCustomProjectId(): void {
  const result = prepareClarifyAnswers(
    [projectQuestion],
    { project_id: CUSTOM_PROJECT_OPTION_VALUE },
    { project_id: '#42' },
  )

  expect(result.missing).toBeUndefined()
  expect(result.answers).toEqual({ project_id: 42 })
})

/** 验证自定义项目名称交给后端 project_query 做模糊解析。 */
it('converts a custom project name to project query', function testCustomProjectName(): void {
  const result = prepareClarifyAnswers(
    [projectQuestion],
    { project_id: CUSTOM_PROJECT_OPTION_VALUE },
    { project_id: ' 皮卡丘漏洞 ' },
  )

  expect(result.missing).toBeUndefined()
  expect(result.answers).toEqual({ project_query: '皮卡丘漏洞' })
})

/** 验证选择其他后未输入内容时仍按必填问题处理。 */
it('rejects an empty custom project answer', function testEmptyCustomProject(): void {
  const result = prepareClarifyAnswers(
    [projectQuestion],
    { project_id: CUSTOM_PROJECT_OPTION_VALUE },
    { project_id: '   ' },
  )

  expect(result.missing).toBe(projectQuestion)
  expect(result.answers).toEqual({})
})
