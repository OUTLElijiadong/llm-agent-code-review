import type { SandboxEnvironment, SandboxEvent, SandboxLanguage } from '@/types/sandbox'

const ACTIVE_STATUSES = new Set(['queued', 'dispatching', 'running', 'finalizing', 'ready', 'stopping'])
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'blocked', 'stopped', 'expired'])
const PROJECT_LANGUAGE_TO_SANDBOX: Record<string, SandboxLanguage> = {
  python: 'python', py: 'python',
  javascript: 'node', js: 'node', typescript: 'node', ts: 'node',
  node: 'node', nodejs: 'node', 'node.js': 'node', vue: 'node', svelte: 'node',
  java: 'java', go: 'go', golang: 'go', php: 'php',
}
const PROJECT_LANGUAGE_COMPACT_ALIASES = Object.entries(PROJECT_LANGUAGE_TO_SANDBOX)
  .map(([alias, runtime]) => [alias.replace(/[^a-z0-9]+/g, ''), runtime] as const)
  .sort((left, right) => right[0].length - left[0].length)

export function projectSandboxLanguage(language?: string | null): SandboxLanguage | null {
  const normalized = (language || '').trim().toLowerCase().replace(/[_-]/g, '')
  const exact = PROJECT_LANGUAGE_TO_SANDBOX[normalized]
  if (exact) return exact
  const compact = normalized.replace(/[^a-z0-9]+/g, '')
  for (const [alias, runtime] of PROJECT_LANGUAGE_COMPACT_ALIASES) {
    const suffix = compact.slice(alias.length)
    if (compact === alias || (compact.startsWith(alias) && /^\d+$/.test(suffix))) return runtime
  }
  return null
}

export function sortSandboxEvents(events: SandboxEvent[]): SandboxEvent[] {
  return [...events].sort((left, right) => {
    if (left.id !== right.id) return left.id - right.id
    return new Date(left.create_time).getTime() - new Date(right.create_time).getTime()
  })
}

export function isSandboxActive(status: string): boolean {
  return ACTIVE_STATUSES.has(status)
}

export function canStopSandbox(status: string): boolean {
  return ACTIVE_STATUSES.has(status) && status !== 'stopping'
}

export function canExtendSandbox(status: string): boolean {
  return ACTIVE_STATUSES.has(status) && status !== 'stopping'
}

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中', dispatching: '调度中', running: '运行中', finalizing: '生成报告中',
  ready: '预览就绪', stopping: '关闭中', succeeded: '已通过', failed: '失败', blocked: '已阻断',
  stopped: '已关闭', expired: '已到期',
}

export function sandboxStatusLabel(status: string): string {
  return STATUS_LABELS[status] || status
}

export interface SandboxConclusionPresentation {
  type: 'success' | 'warning' | 'error'
  title: string
}

export function sandboxConclusionPresentation(environment: SandboxEnvironment): SandboxConclusionPresentation {
  if (environment.status === 'finalizing') {
    return { type: 'warning', title: '确定性结果已生成，审查报告生成中' }
  }
  const summary = environment.result?.summary
  const title = typeof summary === 'string' && summary
    ? summary
    : environment.error || (environment.status === 'ready' ? '部署已启动，可创建预览会话。' : '任务已结束，未返回摘要。')
  return {
    type: environment.status === 'succeeded' || environment.status === 'ready' ? 'success' : 'error',
    title,
  }
}

export function hasSandboxConclusion(environment: SandboxEnvironment): boolean {
  return TERMINAL_STATUSES.has(environment.status) || Object.keys(environment.result || {}).length > 0
}

export function isRemoteAuthorizationRequired(testMode: string, remoteUrl: string): boolean {
  return Boolean(remoteUrl.trim()) && (testMode === 'blackbox' || testMode === 'combined')
}

const STAGE_LABELS: Record<string, string> = {
  authorization: '权限校验', snapshot: '源码快照', worker: '执行器调度', executor: '执行器',
  deploy_verify: '部署核验', agent_tests: '动态测试用例', validating: '沙箱校验', preparing: '环境准备',
  running_whitebox: '白盒测试', starting: '启动应用', health_checking: '健康检查', running: '运行中',
  finalizing: '生成报告',
  stopping: '回收环境', succeeded: '成功', failed: '失败', blocked: '安全阻断', stopped: '已停止',
  conclusion: '测试结论', syntax_repair: '语法修复', multi_agent_review: '多 Agent 审查',
  auto_whitebox: '自动白盒', auto_blackbox: '自动黑盒', auto_smoke: '冒烟测试', stop: '关闭',
  language: '语言识别', dispatch: '调度', progress: '进度', complete: '完成', lifecycle: '生命周期',
  result: '结果', remote_blackbox: '远程黑盒', deploy: '部署',
}

/** Agent 事件阶段英文码 → 中文标签(未收录时原样返回)。 */
export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] || stage
}
