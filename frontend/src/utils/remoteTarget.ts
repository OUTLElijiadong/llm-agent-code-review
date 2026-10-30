/**
 * 远程目标地址的前端规整工具。
 *
 * 小菱团队任务里的远程目标可能带路径、查询串甚至内嵌凭据；界面只展示和传递
 * “协议 + 主机 + 非默认端口”的来源，与后端 `redact_target_origin` 的口径一致。
 */

/** 团队任务 input 中可能携带远程目标的字段，顺序与监督器识别外部目标的字段一致。 */
const TARGET_INPUT_KEYS = ['remote_target_url', 'target_url', 'external_target_url'] as const

/** 团队任务被远程授权闸门拦下时，服务端写入的错误码。 */
export const REMOTE_AUTH_REQUIRED_CODE = 'remote_target_authorization_required'
/** 监督子 Agent 要求当前账号确认；任务含远程目标时同样需要先拿到独立授权。 */
export const SUPERVISOR_CONFIRMATION_CODE = 'supervisor_confirmation_required'

const BLOCKED_TASK_STATUSES = new Set(['blocked', 'failed', 'dead_letter'])
const INTERNAL_HOST_SUFFIXES = ['.local', '.internal', '.localhost']

/**
 * 把任意输入规整为目标来源（形如 `https://example.com/`）。
 * 非字符串、非 http(s) 或无法解析的值返回空串；凭据、路径、查询串和片段一律丢弃。
 */
export function toRemoteTargetOrigin(value: unknown): string {
  if (typeof value !== 'string') return ''
  const raw = value.trim()
  if (!raw) return ''
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return ''
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return ''
  const hostname = url.hostname.replace(/\.+$/, '')
  if (!hostname) return ''
  return `${url.protocol}//${hostname}${url.port ? `:${url.port}` : ''}/`
}

/** 读取团队任务 input 里的远程目标并规整为来源；没有则返回空串。 */
export function remoteTargetFromTaskInput(input: unknown): string {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return ''
  const data = input as Record<string, unknown>
  for (const key of TARGET_INPUT_KEYS) {
    const origin = toRemoteTargetOrigin(data[key])
    if (origin) return origin
  }
  return ''
}

/**
 * 该来源能否通过“远程网站评估”获得授权：仅 HTTPS 默认端口的域名站点根地址，
 * 不接受 IP 与本机/内部域名（与后端 `normalize_target_url` 的限制一致）。
 */
export function isAuthorizableRemoteOrigin(origin: string): boolean {
  if (!origin) return false
  let url: URL
  try {
    url = new URL(origin)
  } catch {
    return false
  }
  if (url.protocol !== 'https:' || url.port) return false
  const host = url.hostname.replace(/\.+$/, '').toLowerCase()
  if (!host || host.includes(':') || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return false
  if (host === 'localhost' || host === 'localhost.localdomain') return false
  return !INTERNAL_HOST_SUFFIXES.some((suffix) => host.endsWith(suffix))
}

export interface RemoteAuthIssue {
  /** 脱敏后的目标来源；服务端与任务输入都没有给出时为空串。 */
  origin: string
  /** 受影响的团队任务标题（按出现顺序去重）。 */
  taskTitles: string[]
  /** 是否可以经远程网站评估申请授权。 */
  authorizable: boolean
}

interface TaskLike {
  title?: string
  status?: string
  input?: unknown
  result?: unknown
  errors?: unknown
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

function errorCodes(task: TaskLike): Array<Record<string, unknown>> {
  return Array.isArray(task.errors) ? task.errors.map(asRecord).filter((item): item is Record<string, unknown> => !!item) : []
}

/** 取这个任务需要提示的远程目标来源；任务不属于“目标未授权”场景时返回 null。 */
function issueOrigin(task: TaskLike): string | null {
  if (!BLOCKED_TASK_STATUSES.has(String(task.status ?? ''))) return null
  const errors = errorCodes(task)
  const gated = errors.find((item) => item.code === REMOTE_AUTH_REQUIRED_CODE)
  const confirmation = errors.some((item) => item.code === SUPERVISOR_CONFIRMATION_CODE)
  const inputOrigin = remoteTargetFromTaskInput(task.input)
  if (gated) {
    const nextAction = asRecord(asRecord(task.result)?.next_action)
    return toRemoteTargetOrigin(gated.target_origin) || toRemoteTargetOrigin(nextAction?.target_url) || inputOrigin
  }
  // 监督确认码只有在任务确实含远程目标时才与授权有关，普通的高风险确认不提示。
  return confirmation && inputOrigin ? inputOrigin : null
}

/** 汇总团队里因远程目标未授权而停住的任务，同一目标合并成一项。 */
export function collectRemoteAuthIssues(tasks: readonly TaskLike[]): RemoteAuthIssue[] {
  const byOrigin = new Map<string, RemoteAuthIssue>()
  for (const task of tasks) {
    const origin = issueOrigin(task)
    if (origin === null) continue
    const issue = byOrigin.get(origin) ?? {
      origin,
      taskTitles: [],
      authorizable: origin === '' || isAuthorizableRemoteOrigin(origin),
    }
    const title = String(task.title ?? '').trim()
    if (title && !issue.taskTitles.includes(title)) issue.taskTitles.push(title)
    byOrigin.set(origin, issue)
  }
  return [...byOrigin.values()]
}
