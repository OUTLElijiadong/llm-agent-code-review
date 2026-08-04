/**
 * ChatAgent 多会话管理(普通用户 chat 与管理员副驾驶共用)。
 * 会话 id 存 localStorage:一个固定索引键 + 每个会话一个快照键。
 */

export interface AgentChatSessionMeta {
  id: string
  title: string
  createdAt: number
}

export interface AgentChatSnapshotMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface AgentChatSnapshot {
  messages: AgentChatSnapshotMessage[]
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

export function saveAgentChatSnapshot(sessionId: string, snapshot: AgentChatSnapshot): void {
  try {
    const trimmed = snapshot.messages.filter((item) => item.content.trim()).slice(-60)
    window.localStorage.setItem(
      SNAPSHOT_PREFIX + sessionId,
      JSON.stringify({ ...snapshot, messages: trimmed }),
    )
  } catch {
    // 存储写满时静默失败,刷新后由服务端检查点兜底。
  }
}
