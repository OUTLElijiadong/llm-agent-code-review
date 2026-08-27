/**
 * 思考城市引擎(纯 TS,不依赖 Vue / DOM,便于单测)
 * ------------------------------------------------------------------
 * 隐喻:小菱的脑子里住着一座知识城市。
 *   大厦     — 一栋一类知识(安全规则/代码模式/历史修复…)
 *   房间亮灯 — 该知识点被「想起」
 *   信使     — 亮灯房间派出的光点,沿道路把词汇运往广场
 *   句子     — 词汇在广场汇合,拼成一句完整的思考
 *
 * 引擎负责:确定性布局(seed 随机)+ 每帧推进 dt + 阶段编排。
 * 渲染层(Canvas)只读状态,不写状态。
 */

export type CityPhase = 'ignite' | 'gather' | 'assemble'

export interface CityWord {
  /** 词汇文本 */
  text: string
  /** 所属大厦 */
  building: number
  /** 词汇掉落时刻(ms) */
  at: number
}

export interface CityBuilding {
  id: number
  /** 主题名 */
  theme: string
  /** 大厦平面坐标与尺寸 */
  x: number
  y: number
  w: number
  h: number
  cols: number
  rows: number
  /** 房间窗口网格单元尺寸 */
  cell: number
  /** 每个房间的点亮时刻(ms),-1 = 尚未点亮 */
  rooms: number[]
  /** 该栋可贡献的词汇 */
  words: string[]
  /** 下一颗信使从第几个窗口出发 */
  nextSpawnRoom: number
  /** 大厦入口(道路汇合点)坐标 */
  doorX: number
  doorY: number
}

export interface CityMessenger {
  id: number
  building: number
  room: number
  word: string
  /** 出发点(窗口中心) */
  x0: number
  y0: number
  /** 折线路径(窗口→门口→沿路→广场) */
  path: Array<{ x: number; y: number }>
  /** 已走路程(px) */
  dist: number
  /** 路径总长(px) */
  total: number
  /** 速度(px/ms) */
  speed: number
  /** 是否已抵达 */
  arrived: boolean
  /** 抵达时刻(ms),用于词语落位动画 */
  arrivedAt: number
}

export interface CitySentence {
  /** 完整目标文本 */
  target: string
  /** 已到达的词 */
  arrived: CityWord[]
  /** 组成完毕时刻(ms),-1 未完成 */
  completedAt: number
}

export interface CityStats {
  /** 已点亮知识房间数 */
  litRooms: number
  /** 全部房间数 */
  totalRooms: number
  /** 知识流:已送达词汇数 */
  delivered: number
  /** 拼成句子数 */
  sentences: number
  /** 历史积累:本次会话开始前就已点亮的房间数 */
  memoryRooms: number
}

/** 子 Agent 城市(多Agent联动):主城市右侧临时长出的微缩城市 */
export interface SubCity {
  /** 对应后端 team_id */
  teamId: number
  /** 标题(团队名) */
  title: string
  /** 子城市中心坐标与尺寸(渲染层计算) */
  x: number
  y: number
  w: number
  h: number
  /** 成员微缩房间:member_key → 点亮时刻(-1 未点亮 / >=0 点亮) */
  rooms: Array<{ memberKey: string; displayName: string; litAt: number }>
  /** 从主城市飞来的光带进度(0-1),1 表示已建成 */
  buildProgress: number
  /** 完成时光点飞回主城市的进度(0-1),-1 未开始 */
  returnProgress: number
  /** 状态(running/completed/failed/…) */
  status: string
  /** 建立时刻(ms) */
  createdAt: number
}

export interface CityState {
  time: number
  phase: CityPhase
  buildings: CityBuilding[]
  messengers: CityMessenger[]
  sentences: CitySentence[]
  stats: CityStats
  /** 广场(词汇汇合点)坐标 */
  plaza: { x: number; y: number }
  /** 全部句子拼完 */
  done: boolean
  /** 近期事件(点亮/送达/成句),供 HUD 滚动播报 */
  events: Array<{ at: number; text: string }>
  /** 多Agent联动:活跃子城市 */
  subCities: SubCity[]
}

export interface CityEngineOptions {
  width: number
  height: number
  seed?: number
  /** 各栋大厦主题与词库 */
  themes?: Array<{ theme: string; words: string[] }>
  /** 目标句子(用词需尽量来自词库) */
  sentences?: string[]
  /** 时间倍率 */
  timeScale?: number
  /** 历史积累:预先点亮的房间 key(格式 `${buildingId}:${roomIdx}`) */
  memoryKeys?: string[]
}

const DEFAULT_THEMES: Array<{ theme: string; words: string[] }> = [
  { theme: '安全规则库', words: ['SQL注入', 'XSS', '越权', '反序列化', 'SSRF', '硬编码密钥', '命令注入', '路径穿越'] },
  { theme: '代码模式馆', words: ['函数', '依赖', '调用链', '数据流', '入口点', '危险汇点', '上下文', '污点'] },
  { theme: '历史修复仓', words: ['修复', '回归', '补丁', '复盘', '加固', '误报', '基线', '白名单'] },
  { theme: '漏洞情报站', words: ['CVE', '情报', '利用链', '披露', '风险', '攻击面', ' payload ', '0day'] },
  { theme: '语义图谱塔', words: ['向量', '检索', '召回', '聚类', '知识块', '关联', '推理', '记忆'] },
]

const DEFAULT_SENTENCES = ['点亮知识房间', '信使沿道路汇合', '词汇拼成完整思路', '回答即将送达']

/** 确定性伪随机(mulberry32) */
export function createRandom(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function clamp(v: number, min: number, max: number): number {
  return v < min ? min : v > max ? max : v
}

/** 大厦网格布局:沿城市地平线一字排开,宽窄高低随机 */
function layoutBuildings(
  rand: () => number,
  width: number,
  height: number,
  themes: Array<{ theme: string; words: string[] }>,
): CityBuilding[] {
  const n = themes.length
  const groundY = height * 0.86 // 地平线
  const margin = width * 0.05
  const usable = width - margin * 2
  const gap = usable * 0.028
  const baseW = (usable - gap * (n - 1)) / n
  const buildings: CityBuilding[] = []
  let cursor = margin
  for (let i = 0; i < n; i++) {
    const w = baseW * (0.86 + rand() * 0.28)
    const cols = clamp(Math.round(w / 15), 3, 5)
    const rows = clamp(5 + Math.floor(rand() * 5) + (i % 2), 5, 10)
    const cell = Math.min(w / (cols + 1.6), (groundY * 0.62) / (rows + 2.2))
    const h = cell * (rows + 1.4)
    const x = cursor + (baseW - w) / 2
    const y = groundY - h
    const roomCount = cols * rows
    buildings.push({
      id: i,
      theme: themes[i].theme,
      x,
      y,
      w,
      h,
      cols,
      rows,
      cell,
      rooms: new Array(roomCount).fill(-1),
      words: themes[i].words,
      nextSpawnRoom: 0,
      doorX: x + w / 2,
      doorY: groundY,
    })
    cursor += baseW + gap
  }
  return buildings
}

/** 信使路径:窗口 → 垂直下楼 → 门口 → 沿地面道路 → 广场 */
function buildPath(b: CityBuilding, roomIdx: number, plaza: { x: number; y: number }): {
  path: Array<{ x: number; y: number }>
  start: { x: number; y: number }
} {
  const col = roomIdx % b.cols
  const row = Math.floor(roomIdx / b.cols)
  const start = {
    x: b.x + b.cell * (col + 1.3),
    y: b.y + b.cell * (row + 1.2),
  }
  const roadY = b.doorY + 9
  const path = [
    start,
    { x: start.x, y: b.doorY },
    { x: b.doorX, y: b.doorY },
    { x: b.doorX, y: roadY },
    { x: plaza.x, y: roadY },
    { x: plaza.x, y: plaza.y },
  ]
  return { path, start }
}

function pathLength(path: Array<{ x: number; y: number }>): number {
  let len = 0
  for (let i = 1; i < path.length; i++) {
    len += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y)
  }
  return len
}

/** 信使当前位置(沿折线按路程插值) */
export function messengerPos(m: CityMessenger): { x: number; y: number } {
  let rest = m.dist
  for (let i = 1; i < m.path.length; i++) {
    const a = m.path[i - 1]
    const b = m.path[i]
    const seg = Math.hypot(b.x - a.x, b.y - a.y)
    if (rest <= seg && seg > 0) {
      const t = rest / seg
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t }
    }
    rest -= seg
  }
  const last = m.path[m.path.length - 1]
  return { x: last.x, y: last.y }
}

/** 从目标句子拆词:优先匹配词库词,否则按 2 字切分 */
export function tokenizeSentence(sentence: string, lexicon: string[]): string[] {
  const out: string[] = []
  let rest = sentence
  const sorted = [...lexicon].sort((a, b) => b.length - a.length)
  while (rest.length) {
    const hit = sorted.find((w) => w.trim() && rest.startsWith(w.trim()))
    if (hit) {
      out.push(hit.trim())
      rest = rest.slice(hit.trim().length)
    } else {
      out.push(rest.slice(0, 2))
      rest = rest.slice(2)
    }
  }
  return out.filter(Boolean)
}

export function createCityEngine(options: CityEngineOptions) {
  const width = options.width
  const height = options.height
  const rand = createRandom(options.seed ?? 20260826)
  const timeScale = options.timeScale ?? 1
  const themes = options.themes ?? DEFAULT_THEMES
  const sentenceTexts = options.sentences ?? DEFAULT_SENTENCES
  const lexicon = themes.flatMap((t) => t.words)

  const buildings = layoutBuildings(rand, width, height, themes)
  const totalRooms = buildings.reduce((sum, b) => sum + b.rooms.length, 0)
  const plaza = { x: width / 2, y: height * 0.955 }

  /** 每个词分配到「拥有它的那栋大厦」,没有则轮流分 */
  const wordOwner = new Map<string, number>()
  sentenceTexts.forEach((s) => {
    tokenizeSentence(s, lexicon).forEach((word) => {
      if (wordOwner.has(word)) return
      const owner = buildings.findIndex((b) => b.words.some((w) => w.trim() === word))
      wordOwner.set(word, owner >= 0 ? owner : Math.floor(rand() * buildings.length))
    })
  })

  const state: CityState = {
    time: 0,
    phase: 'ignite',
    buildings,
    messengers: [],
    sentences: sentenceTexts.map((target) => ({ target, arrived: [], completedAt: -1 })),
    stats: { litRooms: 0, totalRooms, delivered: 0, sentences: 0, memoryRooms: 0 },
    plaza,
    done: false,
    events: [],
    subCities: [],
  }

  // 历史积累:把以往点亮的房间先点亮(发柔和的「已有积累」光,不参与本次节奏)
  const memorySet = new Set(options.memoryKeys ?? [])
  let memoryRooms = 0
  for (const b of buildings) {
    for (let i = 0; i < b.rooms.length; i++) {
      if (memorySet.has(`${b.id}:${i}`)) {
        b.rooms[i] = -2 // -2 = 记忆点亮(区别于 -1 未点亮 / >=0 本次点亮时刻)
        memoryRooms += 1
      }
    }
  }
  state.stats.memoryRooms = memoryRooms

  let messengerSeq = 0
  /** 点亮节奏:开局密,随后放缓 */
  let nextIgniteAt = 120
  /** 信使出发节奏 */
  let nextSpawnAt = 900
  let sentenceCursor = 0
  /** 当前句子的下一目标词 */
  let wordCursor = 0

  function pushEvent(text: string) {
    state.events.push({ at: state.time, text })
    if (state.events.length > 24) state.events.splice(0, state.events.length - 24)
  }

  function igniteRoom() {
    // 只点「完全黑暗」(-1)的房间;-2 是历史记忆,不动
    const candidates = buildings.filter((b) => b.rooms.some((r) => r === -1))
    if (!candidates.length) return
    const b = candidates[Math.floor(rand() * candidates.length)]
    const dark = b.rooms.map((r, i) => (r === -1 ? i : -1)).filter((i) => i >= 0)
    const idx = dark[Math.floor(rand() * dark.length)]
    b.rooms[idx] = state.time
    state.stats.litRooms += 1
    if (state.stats.litRooms % 6 === 0) pushEvent(`「${b.theme}」第 ${idx + 1} 号房间亮起`)
  }

  function spawnMessenger() {
    // 当前句子需要的下一个词
    const sentence = state.sentences[sentenceCursor]
    if (!sentence) return
    const want = tokenizeSentence(sentence.target, lexicon)[wordCursor]
    if (want === undefined) return
    const ownerIdx = wordOwner.get(want) ?? Math.floor(rand() * buildings.length)
    const b = buildings[ownerIdx]
    // 选一个已点亮的房间出发;没有就从任意房间出发并顺手点亮
    const lit = b.rooms.map((r, i) => (r >= 0 ? i : -1)).filter((i) => i >= 0)
    let roomIdx: number
    if (lit.length) {
      roomIdx = lit[b.nextSpawnRoom % lit.length]
    } else {
      roomIdx = b.nextSpawnRoom % b.rooms.length
      b.rooms[roomIdx] = state.time
      state.stats.litRooms += 1
    }
    b.nextSpawnRoom += 1
    const { path, start } = buildPath(b, roomIdx, plaza)
    state.messengers.push({
      id: messengerSeq++,
      building: ownerIdx,
      room: roomIdx,
      word: want,
      x0: start.x,
      y0: start.y,
      path,
      dist: 0,
      total: pathLength(path),
      speed: 0.055 + rand() * 0.03,
      arrived: false,
      arrivedAt: -1,
    })
  }

  const engine = {
    state,
    /**
     * 多Agent联动:注册一个子 Agent 团队,在主城市右侧长出一座子城市。
     * members 为团队成员(member_key/display_name),用于微缩房间。
     */
    spawnSubCity(teamId: number, title: string, members: Array<{ memberKey: string; displayName: string }>): SubCity {
      const existing = state.subCities.find((s) => s.teamId === teamId)
      if (existing) return existing
      // 子城市在主城市广场右上方,尺寸按成员数自适应
      const w = Math.max(64, Math.min(120, 26 + members.length * 14))
      const h = 54
      const x = Math.min(width - w - 10, plaza.x + 70 + state.subCities.length * (w + 12))
      const y = height * 0.86 - h - 8
      const sub: SubCity = {
        teamId,
        title,
        x,
        y,
        w,
        h,
        rooms: members.map((m) => ({ memberKey: m.memberKey, displayName: m.displayName, litAt: -1 })),
        buildProgress: 0,
        returnProgress: -1,
        status: 'running',
        createdAt: state.time,
      }
      state.subCities.push(sub)
      pushEvent(`子 Agent 团队「${title}」出发,在主城旁建起子城市`)
      return sub
    },
    /** 子城市事件:task.claimed 点亮成员房间 */
    subCityTaskStarted(teamId: number, memberKey: string): void {
      const sub = state.subCities.find((s) => s.teamId === teamId)
      if (!sub) return
      const room = sub.rooms.find((r) => r.memberKey === memberKey)
      if (room && room.litAt < 0) {
        room.litAt = state.time
        pushEvent(`「${sub.title}」的 ${room.displayName} 开始工作`)
      }
    },
    /** 子城市事件:team 终态,触发光点飞回 */
    subCityFinished(teamId: number, status: string): void {
      const sub = state.subCities.find((s) => s.teamId === teamId)
      if (!sub || sub.returnProgress >= 0) return
      sub.status = status
      sub.returnProgress = 0
      pushEvent(`「${sub.title}」${status === 'completed' ? '完成,光点飞回主城' : '结束(' + status + ')'}`)
    },
    /** 推进 dt(ms)。返回是否仍在运行(全部句子完成后返回 false 之前的帧都 true) */
    tick(dtMs: number): boolean {
      const dt = clamp(dtMs, 0, 100) * timeScale
      state.time += dt
      const t = state.time

      // 阶段一:点亮知识(全程都在点,只是前期密)
      if (state.stats.litRooms < totalRooms && t >= nextIgniteAt) {
        igniteRoom()
        const progress = state.stats.litRooms / totalRooms
        nextIgniteAt = t + (progress < 0.35 ? 90 : progress < 0.7 ? 240 : 620) / timeScale
      }

      // 阶段切换:点亮超过 18% 或 1.4s 后进入收集
      if (state.phase === 'ignite' && (state.stats.litRooms / totalRooms > 0.18 || t > 1400 * (1 / timeScale))) {
        state.phase = 'gather'
        pushEvent('知识信使出发,沿道路送往广场')
      }

      // 阶段二:信使送词
      if ((state.phase === 'gather' || state.phase === 'assemble') && t >= nextSpawnAt && sentenceCursor < state.sentences.length) {
        spawnMessenger()
        const sentence = state.sentences[sentenceCursor]
        const tokens = tokenizeSentence(sentence.target, lexicon)
        wordCursor += 1
        if (wordCursor >= tokens.length) {
          wordCursor = 0
          sentenceCursor += 1
          if (state.phase === 'gather' && sentenceCursor >= Math.max(1, state.sentences.length - 1)) {
            state.phase = 'assemble'
          }
        }
        nextSpawnAt = t + 380 / timeScale
      }

      // 推进信使
      for (const m of state.messengers) {
        if (m.arrived) continue
        m.dist += m.speed * dt
        if (m.dist >= m.total) {
          m.arrived = true
          m.arrivedAt = t
          state.stats.delivered += 1
          // 词落到第一句还缺它的句子上
          const target = state.sentences.find((s) => {
            if (s.completedAt >= 0) return false
            const tokens = tokenizeSentence(s.target, lexicon)
            return tokens.length > s.arrived.length && tokens[s.arrived.length] === m.word
          }) ?? state.sentences.find((s) => s.completedAt < 0)
          if (target) {
            target.arrived.push({ text: m.word, building: m.building, at: t })
            const tokens = tokenizeSentence(target.target, lexicon)
            if (target.arrived.length >= tokens.length) {
              target.completedAt = t
              state.stats.sentences += 1
              pushEvent(`思路成型:「${target.target}」`)
            }
          }
        }
      }
      // 清理旧信使(抵达 1.6s 后淡出移除)
      if (state.messengers.length > 40) {
        state.messengers = state.messengers.filter((m) => !m.arrived || t - m.arrivedAt < 1600)
      }

      // 全部句子完成 → 继续点亮剩余房间(知识积累不止),但标记 done
      if (!state.done && state.sentences.every((s) => s.completedAt >= 0)) {
        state.done = true
        pushEvent('回答组装完毕,小菱这就说')
      }

      // 多Agent联动:推进子城市建造/飞回
      for (const sub of state.subCities) {
        if (sub.buildProgress < 1) {
          sub.buildProgress = Math.min(1, sub.buildProgress + dt / 900)
        }
        if (sub.returnProgress >= 0 && sub.returnProgress < 1) {
          sub.returnProgress = Math.min(1, sub.returnProgress + dt / 1400)
        }
      }
      // 飞回完成后淡出移除
      if (state.subCities.some((s) => s.returnProgress >= 1)) {
        state.subCities = state.subCities.filter((s) => s.returnProgress < 1)
      }
      return true
    },
    /** 导出本次点亮的房间 key(不含历史记忆),用于写回长期积累 */
    collectSessionLitKeys(): string[] {
      const keys: string[] = []
      for (const b of buildings) {
        for (let i = 0; i < b.rooms.length; i++) {
          if (b.rooms[i] >= 0) keys.push(`${b.id}:${i}`)
        }
      }
      return keys
    },
    /** 重置(换一句话/重新思考);历史记忆房间保留 */
    reset() {
      state.time = 0
      state.phase = 'ignite'
      state.messengers = []
      state.sentences.forEach((s) => {
        s.arrived = []
        s.completedAt = -1
      })
      state.stats = { litRooms: 0, totalRooms, delivered: 0, sentences: 0, memoryRooms: state.stats.memoryRooms }
      state.done = false
      state.events = []
      state.subCities = []
      buildings.forEach((b) => {
        for (let i = 0; i < b.rooms.length; i++) {
          if (b.rooms[i] >= 0) b.rooms[i] = -1 // 本次点亮的熄灭,历史记忆(-2)保留
        }
        b.nextSpawnRoom = 0
      })
      messengerSeq = 0
      nextIgniteAt = 120
      nextSpawnAt = 900
      sentenceCursor = 0
      wordCursor = 0
    },
  }
  return engine
}

export type CityEngine = ReturnType<typeof createCityEngine>
