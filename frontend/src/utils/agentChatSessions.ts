/**
 * ChatAgent 多会话管理(普通用户 chat 与管理员副驾驶共用)。
 * 会话 id 存 localStorage:一个固定索引键 + 每个会话一个快照键。
 */

import { isAgentResponseSessionOccupied } from '@/utils/agentResponseSession'

export interface AgentChatSessionMeta {
  id: string
  title: string
  createdAt: number
  /** 置顶状态仅存本地,用于切换器排序;服务端恢复时保留缓存值。 */
  pinned?: boolean
}

/**
 * Agent Mesh 会话发现的最小字段；故意不依赖 API 模块，避免 localStorage 工具和网络层互相耦合。
 */
export interface DiscoveredAgentChatSession {
  id: string
  title?: string
  surface: 'user' | 'admin'
  kind: 'session'
  lastSeenAt?: string
  /** 服务端 agent_response_run 账本给出的权威运行状态。 */
  activeRunId?: string
  activeRunStatus?: string
}

export interface AgentChatSnapshotMessage {
  role: 'user' | 'assistant'
  content: string
  /** 消息发起时已发现的子 Agent 团队,用于关闭/重开后恢复卡片位置。 */
  teamIds?: number[]
}

export interface AgentChatSnapshotTeam {
  team_id: number
  title: string
  objective?: string
  surface: 'user' | 'admin'
  session_id: string
  status: string
  max_active_children: number
  trace_id: string
  counts?: { total: number; completed: number; running: number; queued: number; failed: number; blocked: number }
  created_at?: string
  updated_at?: string
}

export interface AgentChatSnapshot {
  messages: AgentChatSnapshotMessage[]
  /** 团队 API 暂时不可用时仍可恢复卡片标题、状态和进度。 */
  teams?: AgentChatSnapshotTeam[]
  runStatus: string | null
  updatedAt: number
}

const INDEX_PREFIX = 'prism-agent-sessions:'
const SNAPSHOT_PREFIX = 'prism-agent-session-snapshot:'

function readIndex(storageKey: string): AgentChatSessionMeta[] {
  try {
    const raw = window.localStorage.getItem(INDEX_PREFIX + storageKey)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is AgentChatSessionMeta => (
      typeof item === 'object'
      && item !== null
      && typeof (item as AgentChatSessionMeta).id === 'string'
      && typeof (item as AgentChatSessionMeta).title === 'string'
    ))
  } catch {
    return []
  }
}

function writeIndex(storageKey: string, metas: AgentChatSessionMeta[]): void {
  try {
    window.localStorage.setItem(INDEX_PREFIX + storageKey, JSON.stringify(metas.slice(0, 30)))
  } catch {
    // 存储写满时静默失败,会话功能降级为单会话。
  }
}

function migrateLegacy(storageKey: string, legacyKey: string, idPrefix: string): AgentChatSessionMeta[] {
  const legacy = window.localStorage.getItem(legacyKey)
  const metas: AgentChatSessionMeta[] = [{
    id: legacy || `${idPrefix}-${crypto.randomUUID()}`,
    title: '默认对话',
    createdAt: Date.now(),
  }]
  writeIndex(storageKey, metas)
  window.localStorage.removeItem(legacyKey)
  return metas
}

export function loadAgentChatSessions(
  storageKey: string,
  legacyKey: string,
  idPrefix: string,
): AgentChatSessionMeta[] {
  const existing = readIndex(storageKey)
  if (existing.length) return existing
  const migrated = migrateLegacy(storageKey, legacyKey, idPrefix)
  return migrated
}

/** 将服务端发现结果持久化为当前 surface 的本地索引。 */
export function saveAgentChatSessions(storageKey: string, metas: AgentChatSessionMeta[]): void {
  writeIndex(storageKey, metas)
}

/**
 * 将服务端 Agent Mesh 会话与本地标题/顺序缓存合并。
 *
 * 服务端是会话集合的事实源；本地只用于已发现会话的标题和最近顺序。
 * preserveIds 仅用于首次心跳尚未落库的当前会话，下一次刷新会按服务端集合清理。
 */
export function mergeAgentChatSessions(
  local: AgentChatSessionMeta[],
  discovered: DiscoveredAgentChatSession[],
  surface: 'user' | 'admin',
  preserveIds: ReadonlySet<string> = new Set(),
): AgentChatSessionMeta[] {
  const remote = discovered.filter((item) => (
    item.kind === 'session'
    && item.surface === surface
    && typeof item.id === 'string'
    && item.id.length > 0
  ))
  const remoteById = new Map(remote.map((item) => [item.id, item]))
  const result: AgentChatSessionMeta[] = []
  const included = new Set<string>()

  const toMeta = (item: DiscoveredAgentChatSession, cached?: AgentChatSessionMeta): AgentChatSessionMeta => {
    const parsedLastSeen = item.lastSeenAt ? Date.parse(item.lastSeenAt) : Number.NaN
    return {
      id: item.id,
      // 本地标题包含用户自动命名；没有缓存时才采用服务端标题。
      title: cached?.title?.trim() || item.title?.trim() || '默认对话',
      createdAt: cached?.createdAt ?? (Number.isFinite(parsedLastSeen) ? parsedLastSeen : Date.now()),
      pinned: cached?.pinned === true,
    }
  }

  // 先按本地顺序输出仍被服务端发现的会话，保留最近使用顺序。
  for (const cached of local) {
    const item = remoteById.get(cached.id)
    if (!item || included.has(item.id)) continue
    result.push(toMeta(item, cached))
    included.add(item.id)
  }

  // 服务端新发现的会话按服务端 last_seen 顺序追加。
  for (const item of remote) {
    if (included.has(item.id)) continue
    result.push(toMeta(item))
    included.add(item.id)
  }

  // 首次发现可能早于当前会话的 heartbeat；只临时保留当前会话，避免切换器丢失当前上下文。
  for (const cached of local) {
    if (!preserveIds.has(cached.id) || included.has(cached.id)) continue
    result.unshift(cached)
    included.add(cached.id)
  }

  return result.slice(0, 30)
}

export function createAgentChatSession(
  storageKey: string,
  idPrefix: string,
  title = '新对话',
): AgentChatSessionMeta {
  const meta: AgentChatSessionMeta = {
    id: `${idPrefix}-${crypto.randomUUID()}`,
    title,
    createdAt: Date.now(),
  }
  writeIndex(storageKey, [meta, ...readIndex(storageKey)])
  return meta
}

export function renameAgentChatSession(
  storageKey: string,
  sessionId: string,
  title: string,
): void {
  const metas = readIndex(storageKey)
  const target = metas.find((item) => item.id === sessionId)
  if (!target) return
  target.title = title.slice(0, 24)
  writeIndex(storageKey, metas)
}

/** 设置会话置顶标记并写回本地索引。 */
export function setAgentChatSessionPinned(
  storageKey: string,
  sessionId: string,
  pinned: boolean,
): void {
  const metas = readIndex(storageKey)
  const target = metas.find((item) => item.id === sessionId)
  if (!target) return
  target.pinned = pinned
  writeIndex(storageKey, metas)
}

/**
 * 新对话自动命名:仅当会话仍是占位标题(新对话/默认对话)时,
 * 用首条用户消息提炼一个标题。返回是否已命名。
 */
export function autoTitleAgentChatSession(
  storageKey: string,
  sessionId: string,
  firstUserText: string,
): boolean {
  const metas = readIndex(storageKey)
  const target = metas.find((item) => item.id === sessionId)
  if (!target) return false
  if (target.title !== '新对话' && target.title !== '默认对话') return false
  const cleaned = firstUserText.replace(/\s+/g, ' ').trim()
  if (!cleaned) return false
  target.title = cleaned.length > 18 ? `${cleaned.slice(0, 18)}…` : cleaned
  writeIndex(storageKey, metas)
  return true
}

export function removeAgentChatSession(storageKey: string, sessionId: string): void {
  writeIndex(storageKey, readIndex(storageKey).filter((item) => item.id !== sessionId))
  try {
    window.localStorage.removeItem(SNAPSHOT_PREFIX + sessionId)
  } catch {
    // 忽略存储异常
  }
}

export function loadAgentChatSnapshot(sessionId: string): AgentChatSnapshot | null {
  try {
    const raw = window.localStorage.getItem(SNAPSHOT_PREFIX + sessionId)
    if (!raw) return null
    const parsed = JSON.parse(raw) as AgentChatSnapshot
    if (!parsed || !Array.isArray(parsed.messages)) return null
    return parsed
  } catch {
    return null
  }
}

/** 会话标题是否仍为未命名的占位标题。 */
export function isPlaceholderAgentChatTitle(title: string): boolean {
  return title === '新对话' || title === '默认对话'
}

export function isPristineAgentChatSession(sessionId: string, welcomeText: string, title = ''): boolean {
  const snapshot = loadAgentChatSnapshot(sessionId)
  // 无快照时只有占位标题才是可复用的空会话。
  // 旧调用不传 title,按占位处理以保持兼容。
  if (!snapshot) return title === '' || isPlaceholderAgentChatTitle(title)
  if (isAgentResponseSessionOccupied(snapshot.runStatus)) return false
  return !snapshot.messages.some((message) => (
    message.content.trim().length > 0 && message.content.trim() !== welcomeText.trim()
  ))
}

/** 复用既有空会话，避免用户关闭未输入的新对话后不断生成空白条目。 */
export function findPristineAgentChatSession(
  sessions: AgentChatSessionMeta[],
  welcomeText: string,
  busyIds: ReadonlySet<string> = new Set(),
): AgentChatSessionMeta | undefined {
  return sessions.find((session) => (
    !busyIds.has(session.id)
    && isPristineAgentChatSession(session.id, welcomeText, session.title)
  ))
}

export function saveAgentChatSnapshot(sessionId: string, snapshot: AgentChatSnapshot): void {
  try {
    // 空的时间线消息也可能携带团队锚点,不能在快照压缩时丢掉。
    const trimmed = snapshot.messages.filter((item) => item.content.trim() || item.teamIds?.length).slice(-60)
    window.localStorage.setItem(
      SNAPSHOT_PREFIX + sessionId,
      JSON.stringify({ ...snapshot, messages: trimmed }),
    )
  } catch {
    // 存储写满时静默失败,刷新后由服务端检查点兜底。
  }
}
