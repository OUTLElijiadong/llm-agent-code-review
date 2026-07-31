import type { ClarifyQuestion } from '@/types/agentEvent'

export const CUSTOM_PROJECT_OPTION_VALUE = '__custom_project__'

export interface ClarifyProjectOption {
  value: string | number
  label: string
}

export interface PreparedClarifyAnswers {
  answers: Record<string, string | number>
  missing?: ClarifyQuestion
}

/**
 * 合并后端推荐项目与项目 API 查询结果，并在末尾补充自定义输入入口。
 * @param recommended - Clarify 响应携带的推荐候选。
 * @param searched - 项目 API 首屏或关键词查询结果。
 * @returns 按项目值去重后的选项，推荐候选优先且“其他”固定在末尾。
 */
export function mergeProjectClarifyOptions(
  recommended: ClarifyProjectOption[] = [],
  searched: ClarifyProjectOption[] = [],
): ClarifyProjectOption[] {
  const merged: ClarifyProjectOption[] = []
  const seen = new Set<string>()
  for (const option of [...recommended, ...searched]) {
    if (option.value === CUSTOM_PROJECT_OPTION_VALUE) continue
    const key = String(option.value)
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(option)
  }
  merged.push({
    value: CUSTOM_PROJECT_OPTION_VALUE,
    label: '其他（自定义输入）',
  })
  return merged
}

/**
 * 根据搜索关键词选择项目下拉的数据源并生成最终选项。
 * @param recommended - 后端 Clarify 初始推荐候选。
 * @param initial - 项目 API 无关键词首屏结果。
 * @param searched - 项目 API 按当前关键词查询的结果。
 * @param keyword - 当前选择器搜索关键词。
 * @returns 无关键词时合并推荐和首屏；有关键词时仅保留远程匹配和自定义入口。
 */
export function resolveProjectClarifyOptions(
  recommended: ClarifyProjectOption[] = [],
  initial: ClarifyProjectOption[] = [],
  searched: ClarifyProjectOption[] = [],
  keyword = '',
): ClarifyProjectOption[] {
  if (keyword.trim()) return mergeProjectClarifyOptions([], searched)
  return mergeProjectClarifyOptions(recommended, initial)
}

/**
 * 判断 Clarify 答案是否缺少必填值。
 * @param value - 当前问题答案。
 * @returns 空字符串、null 或 undefined 时返回 true。
 */
function isEmptyAnswer(value: unknown): boolean {
  return value === '' || value === null || value === undefined
}

/**
 * 把项目选择器答案转换为后端 Clarify 协议可接受的字段。
 * @param questions - 当前 Clarify 问题列表。
 * @param sourceAnswers - 普通控件收集到的答案。
 * @param customProjectInputs - 选择“其他”后填写的项目名称或 ID。
 * @returns 可提交答案；必填项缺失时同时返回对应问题。
 */
export function prepareClarifyAnswers(
  questions: ClarifyQuestion[],
  sourceAnswers: Record<string, string | number>,
  customProjectInputs: Record<string, string>,
): PreparedClarifyAnswers {
  const answers = { ...sourceAnswers }
  for (const question of questions) {
    const value = sourceAnswers[question.key]
    if (question.type === 'select_project' && value === CUSTOM_PROJECT_OPTION_VALUE) {
      delete answers[question.key]
      const customValue = (customProjectInputs[question.key] ?? '').trim()
      if (!customValue) return { answers, missing: question }
      const numericValue = customValue.replace(/^#/, '')
      if (/^\d+$/.test(numericValue)) {
        answers[question.key] = Number(numericValue)
      } else {
        answers.project_query = customValue
      }
      continue
    }
    if (question.required && isEmptyAnswer(value)) {
      return { answers, missing: question }
    }
  }
  return { answers }
}
