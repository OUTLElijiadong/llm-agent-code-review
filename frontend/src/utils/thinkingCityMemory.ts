/**
 * 思考城市长期记忆(按用户持久化)
 * ------------------------------------------------------------------
 * 把小菱每次思考时点亮的知识房间记下来,下次打开城市时先点亮它们,
 * 形成「这个城市越用越大、越用越熟」的积累感。
 *
 * 存储:localStorage,按用户 id 分 key,保留最近 600 块,超出 LRU 淘汰。
 * 仅前端本地,不上传服务器;清缓存即重置。
 */

const KEY_PREFIX = 'prism:thinking-city-memory:'
const MAX_ENTRIES = 600

export interface CityMemoryEntry {
  /** 房间 key,格式 `${buildingId}:${roomIdx}` */
  key: string
  /** 所属大厦主题 */
  theme: string
  /** 代表的知识点(词库词) */
  label: string
  /** 累计点亮次数 */
  count: number
  /** 首次点亮时间戳(ms) */
  firstAt: number
  /** 最近一次点亮时间戳(ms) */
  lastAt: number
}

interface CityMemoryFile {
  version: 1
  entries: Record<string, CityMemoryEntry>
}

function storageKey(userId: number | string | null | undefined): string {
  return `${KEY_PREFIX}${userId ?? 'guest'}`
}

function load(userId: number | string | null | undefined): CityMemoryFile {
  try {
    const raw = localStorage.getItem(storageKey(userId))
    if (!raw) return { version: 1, entries: {} }
    const parsed = JSON.parse(raw) as CityMemoryFile
    if (parsed?.version !== 1 || typeof parsed.entries !== 'object') return { version: 1, entries: {} }
    return parsed
  } catch {
    return { version: 1, entries: {} }
  }
}

function save(userId: number | string | null | undefined, file: CityMemoryFile): void {
  try {
    // LRU:超出上限时淘汰最久没点亮的
    const entries = Object.values(file.entries)
    if (entries.length > MAX_ENTRIES) {
      entries.sort((a, b) => a.lastAt - b.lastAt)
      const drop = entries.slice(0, entries.length - MAX_ENTRIES)
      for (const e of drop) delete file.entries[e.key]
    }
    localStorage.setItem(storageKey(userId), JSON.stringify(file))
  } catch {
    // 隐私模式 / 超容量时静默放弃,不影响思考动画
  }
}

/** 读取某用户的记忆房间 key 列表(用于引擎预点亮) */
export function getMemoryKeys(userId: number | string | null | undefined): string[] {
  return Object.keys(load(userId).entries)
}

/** 读取完整记忆(用于知识卡片) */
export function getMemoryEntries(userId: number | string | null | undefined): CityMemoryEntry[] {
  return Object.values(load(userId).entries).sort((a, b) => b.lastAt - a.lastAt)
}

/**
 * 把本次思考点亮的房间写回记忆。
 * @param litKeys 本次点亮的房间 key
 * @param resolveMeta 由 key 解析出主题/知识点标签的回调(组件侧从引擎建筑信息提供)
 */
export function commitSessionMemory(
  userId: number | string | null | undefined,
  litKeys: string[],
  resolveMeta: (key: string) => { theme: string; label: string } | undefined,
  now = Date.now(),
): void {
  if (!litKeys.length) return
  const file = load(userId)
  for (const key of litKeys) {
    const meta = resolveMeta(key)
    const existing = file.entries[key]
    if (existing) {
      existing.count += 1
      existing.lastAt = now
    } else {
      file.entries[key] = {
        key,
        theme: meta?.theme ?? '知识块',
        label: meta?.label ?? key,
        count: 1,
        firstAt: now,
        lastAt: now,
      }
    }
  }
  save(userId, file)
}

/** 清空某用户的记忆(调试/重置用) */
export function clearMemory(userId: number | string | null | undefined): void {
  try {
    localStorage.removeItem(storageKey(userId))
  } catch {
    /* ignore */
  }
}
