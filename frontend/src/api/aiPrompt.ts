import { get, post } from './http'
import type {
  AiPromptBundleOut,
  AiPromptIssueIn,
  AiPromptProjectIn,
  AiPromptTaskIn,
  AiPromptToolOut,
} from '@/types/aiPrompt'

/**
 * 获取支持的目标 AI 工具枚举(供前端下拉)
 */
export function listAiPromptTools(): Promise<AiPromptToolOut[]> {
  return get<AiPromptToolOut[]>('/ai-prompt/tools')
}

/**
 * 为单条问题生成可粘贴给外部 AI 的修复提示词
 */
export function generatePromptForIssue(payload: AiPromptIssueIn): Promise<AiPromptBundleOut> {
  return post<AiPromptBundleOut>('/ai-prompt/issue', payload)
}

/**
 * 为整个审查任务批量生成提示词
 */
export function generatePromptForTask(payload: AiPromptTaskIn): Promise<AiPromptBundleOut> {
  return post<AiPromptBundleOut>('/ai-prompt/task', payload)
}

/**
 * 为整个项目生成 AI 修复手册(按严重度优先取 top_n 条问题)
 */
export function generatePromptForProject(payload: AiPromptProjectIn): Promise<AiPromptBundleOut> {
  return post<AiPromptBundleOut>('/ai-prompt/project', payload)
}
