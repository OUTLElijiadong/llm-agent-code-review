<script setup lang="ts">
/**
 * 思考城市(ThinkingCity)· 小菱思考可视化
 * ----------------------------------------------------------
 * 把小菱「在想」的过程画成一座知识城市:
 *   大厦     — 一类知识(安全规则库/代码模式馆/历史修复仓…)
 *   房间亮灯 — 知识点被逐个点亮(知识积累可视化)
 *   信使     — 亮灯房间派出的光点,沿道路把词汇运往广场
 *   句子     — 词汇在广场汇合,拼成一句完整思路
 *
 * 渲染:Canvas 2D,动画只画发光点/渐变,不做 DOM 动画;
 * 逻辑:thinkingCityEngine(纯 TS,确定性 seed,可单测)。
 * prefers-reduced-motion: 渲染一帧静态全景,不启动 rAF。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  createCityEngine,
  messengerPos,
  tokenizeSentence,
  type CityEngine,
  type CityPhase,
} from '@/utils/thinkingCityEngine'
import { commitSessionMemory, getMemoryEntries, getMemoryKeys } from '@/utils/thinkingCityMemory'
import { useUserStore } from '@/stores/user'

interface Props {
  /** 思考是否进行中;false 时定格最后一帧并停止 rAF */
  active?: boolean
  /** 紧凑模式(嵌入消息气泡),降低高度与字号 */
  compact?: boolean
  /** 自定义目标句子(小菱正在组织的话) */
  sentences?: string[]
}

const props = withDefaults(defineProps<Props>(), {
  active: true,
  compact: false,
  sentences: undefined,
})

const canvasRef = ref<HTMLCanvasElement | null>(null)
const wrapRef = ref<HTMLElement | null>(null)
/** HUD 用响应式快照(每 ~120ms 从引擎同步一次,避免每帧触发 Vue 更新) */
const litRooms = ref(0)
const totalRooms = ref(0)
const delivered = ref(0)
const doneSentences = ref(0)
const memoryRooms = ref(0)
const phase = ref<CityPhase>('ignite')
const lastEvent = ref('')
/** 已拼成的句子文本(逐句点亮) */
const sentenceLines = ref<Array<{ target: string; arrived: string[]; done: boolean }>>([])
/** 当前选中的知识卡片(点击亮灯房间弹出) */
const selectedRoom = ref<{ key: string; theme: string; label: string; count: number; firstAt: number; lastAt: number } | null>(null)

const userStore = useUserStore()
/** 当前用户 id,用于按人隔离思考城市记忆 */
const memoryUserId = computed(() => userStore.profile?.id ?? 'guest')

let engine: CityEngine | null = null
let rafId = 0
let lastFrameAt = 0
let resizeObserver: ResizeObserver | null = null
let hudSyncAt = 0
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

const phaseLabel = computed(() => {
  if (phase.value === 'ignite') return '正在点亮知识房间'
  if (phase.value === 'gather') return '信使沿道路汇合词汇'
  return '在广场拼成完整思路'
})

/** 建筑夜色配色(与 AiOrb 光谱同族:紫/青/金) */
const PALETTE = {
  sky: ['#171332', '#1d1842', '#241d52'] as const,
  building: '#221e4d',
  buildingEdge: 'rgba(142, 136, 245, 0.28)',
  roomDark: 'rgba(38, 34, 84, 0.9)',
  roomLit: ['#8E88F5', '#7EE3F0', '#D4A53A', '#B85AC4'] as const,
  /** 历史记忆房间的柔光色(暖白,区别于本次点亮的彩色) */
  roomMemory: 'rgba(232, 230, 254, 0.85)',
  road: 'rgba(142, 136, 245, 0.16)',
  roadDash: 'rgba(126, 227, 240, 0.32)',
  messenger: '#7EE3F0',
  plaza: '#8E88F5',
  star: 'rgba(255, 255, 255, 0.5)',
}

/** 星星(静态随机,不随帧变化) */
let stars: Array<{ x: number; y: number; r: number; tw: number }> = []

function initEngine(width: number, height: number): void {
  engine = createCityEngine({
    width,
    height,
    seed: 20260826,
    sentences: props.sentences,
    memoryKeys: getMemoryKeys(memoryUserId.value),
  })
  totalRooms.value = engine.state.stats.totalRooms
  memoryRooms.value = engine.state.stats.memoryRooms
  stars = []
  const rand = (() => {
    let s = 7
    return () => {
      s = (s * 16807) % 2147483647
      return s / 2147483647
    }
  })()
  for (let i = 0; i < 42; i++) {
    stars.push({ x: rand() * width, y: rand() * height * 0.5, r: 0.6 + rand() * 1.2, tw: rand() * Math.PI * 2 })
  }
}

/** 由房间 key 反查主题与知识点标签(用于记忆写回和知识卡片) */
function resolveRoomMeta(key: string): { theme: string; label: string } | undefined {
  const eng = engine
  if (!eng) return undefined
  const [bidStr, idxStr] = key.split(':')
  const b = eng.state.buildings[Number(bidStr)]
  const idx = Number(idxStr)
  if (!b || Number.isNaN(idx)) return undefined
  // 知识点标签:用该房间编号映射到大厦词库的一个词
  const label = b.words[idx % b.words.length]?.trim() || `${b.theme} · ${idx + 1} 号`
  return { theme: b.theme, label }
}

/** 把本次思考点亮的房间写回长期记忆 */
function flushSessionMemory(): void {
  const eng = engine
  if (!eng) return
  const keys = eng.collectSessionLitKeys()
  if (!keys.length) return
  commitSessionMemory(memoryUserId.value, keys, resolveRoomMeta)
}

/** 按 DPR 适配画布尺寸 */
function fitCanvas(): void {
  const canvas = canvasRef.value
  const wrap = wrapRef.value
  if (!canvas || !wrap) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = wrap.clientWidth
  const h = props.compact ? 148 : 210
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.height = `${h}px`
  const ctx = canvas.getContext('2d')
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  // 尺寸变化后按新宽高重建城市布局
  initEngine(w, h)
  hudSyncAt = 0
  if (reducedMotion) drawFrame(0)
}

function drawFrame(dt: number): void {
  const canvas = canvasRef.value
  const eng = engine
  if (!canvas || !eng) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  const s = eng.state

  // 天空
  const sky = ctx.createLinearGradient(0, 0, 0, h)
  sky.addColorStop(0, PALETTE.sky[0])
  sky.addColorStop(0.55, PALETTE.sky[1])
  sky.addColorStop(1, PALETTE.sky[2])
  ctx.fillStyle = sky
  ctx.fillRect(0, 0, w, h)

  // 星星(缓慢闪烁)
  for (const star of stars) {
    const twinkle = reducedMotion ? 0.7 : 0.45 + 0.55 * Math.abs(Math.sin(s.time / 900 + star.tw))
    ctx.globalAlpha = twinkle * 0.8
    ctx.fillStyle = PALETTE.star
    ctx.beginPath()
    ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.globalAlpha = 1

  // 道路(地面一层)
  const roadY = h * 0.86 + 9
  ctx.strokeStyle = PALETTE.road
  ctx.lineWidth = 7
  ctx.beginPath()
  ctx.moveTo(0, roadY)
  ctx.lineTo(w, roadY)
  ctx.stroke()
  // 道路中线虚线缓慢流动
  ctx.strokeStyle = PALETTE.roadDash
  ctx.lineWidth = 1
  ctx.setLineDash([7, 11])
  ctx.lineDashOffset = reducedMotion ? 0 : -(s.time / 28)
  ctx.beginPath()
  ctx.moveTo(0, roadY)
  ctx.lineTo(w, roadY)
  ctx.stroke()
  ctx.setLineDash([])

  // 大厦
  for (const b of s.buildings) {
    // 楼体
    ctx.fillStyle = PALETTE.building
    ctx.strokeStyle = PALETTE.buildingEdge
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.roundRect(b.x, b.y, b.w, b.h, 3)
    ctx.fill()
    ctx.stroke()
    // 楼顶天线
    ctx.strokeStyle = PALETTE.buildingEdge
    ctx.beginPath()
    ctx.moveTo(b.x + b.w / 2, b.y)
    ctx.lineTo(b.x + b.w / 2, b.y - 7)
    ctx.stroke()
    if (!reducedMotion) {
      const blink = 0.4 + 0.6 * Math.abs(Math.sin(s.time / 700 + b.id))
      ctx.globalAlpha = blink
      ctx.fillStyle = '#ff7b93'
      ctx.beginPath()
      ctx.arc(b.x + b.w / 2, b.y - 8, 1.4, 0, Math.PI * 2)
      ctx.fill()
      ctx.globalAlpha = 1
    }
    // 房间窗格
    for (let i = 0; i < b.rooms.length; i++) {
      const col = i % b.cols
      const row = Math.floor(i / b.cols)
      const wx = b.x + b.cell * (col + 0.85)
      const wy = b.y + b.cell * (row + 0.75)
      const litAt = b.rooms[i]
      if (litAt >= 0) {
        // 本次点亮:常亮 + 轻微呼吸;刚点亮 600ms 内做一次 pop 放大
        const age = s.time - litAt
        const pop = age < 600 && !reducedMotion ? 0.6 + 0.4 * (age / 600) : 1
        const breathe = reducedMotion ? 1 : 0.82 + 0.18 * Math.sin(s.time / 520 + i * 1.7)
        const size = b.cell * 0.52 * pop
        ctx.globalAlpha = Math.min(1, 0.35 + 0.65 * breathe)
        ctx.fillStyle = PALETTE.roomLit[i % PALETTE.roomLit.length]
        ctx.shadowColor = ctx.fillStyle
        ctx.shadowBlur = 6
        ctx.beginPath()
        ctx.roundRect(wx + (b.cell * 0.52 - size) / 2, wy + (b.cell * 0.52 - size) / 2, size, size, 1.2)
        ctx.fill()
        ctx.shadowBlur = 0
        ctx.globalAlpha = 1
      } else if (litAt === -2) {
        // 历史记忆:柔和常亮,亮度更低、偏暖,表示「已经积累过」
        const glow = reducedMotion ? 0.5 : 0.42 + 0.1 * Math.sin(s.time / 900 + i * 0.9)
        ctx.globalAlpha = glow
        ctx.fillStyle = PALETTE.roomMemory
        ctx.beginPath()
        ctx.roundRect(wx + b.cell * 0.08, wy + b.cell * 0.08, b.cell * 0.36, b.cell * 0.36, 1)
        ctx.fill()
        ctx.globalAlpha = 1
      } else {
        ctx.fillStyle = PALETTE.roomDark
        ctx.beginPath()
        ctx.roundRect(wx, wy, b.cell * 0.52, b.cell * 0.52, 1.2)
        ctx.fill()
      }
    }
    // 楼名
    ctx.fillStyle = 'rgba(183, 179, 251, 0.62)'
    ctx.font = '9px "JetBrains Mono", monospace'
    ctx.textAlign = 'center'
    ctx.fillText(b.theme, b.x + b.w / 2, b.doorY + 22)
  }

  // 广场(词汇汇合点)
  const plazaPulse = reducedMotion ? 1 : 1 + 0.12 * Math.sin(s.time / 300)
  const plazaR = 10 * plazaPulse
  const pg = ctx.createRadialGradient(s.plaza.x, s.plaza.y, 1, s.plaza.x, s.plaza.y, plazaR * 2.6)
  pg.addColorStop(0, 'rgba(142, 136, 245, 0.85)')
  pg.addColorStop(0.5, 'rgba(61, 188, 217, 0.30)')
  pg.addColorStop(1, 'rgba(61, 188, 217, 0)')
  ctx.fillStyle = pg
  ctx.beginPath()
  ctx.arc(s.plaza.x, s.plaza.y, plazaR * 2.6, 0, Math.PI * 2)
  ctx.fill()
  ctx.fillStyle = PALETTE.plaza
  ctx.beginPath()
  ctx.arc(s.plaza.x, s.plaza.y, 3.4, 0, Math.PI * 2)
  ctx.fill()

  // 信使(发光点 + 拖尾)
  for (const m of s.messengers) {
    if (m.arrived) {
      // 抵达涟漪
      const age = s.time - m.arrivedAt
      if (age < 500 && !reducedMotion) {
        ctx.globalAlpha = 1 - age / 500
        ctx.strokeStyle = PALETTE.messenger
        ctx.lineWidth = 1.2
        ctx.beginPath()
        ctx.arc(s.plaza.x, s.plaza.y, 4 + (age / 500) * 16, 0, Math.PI * 2)
        ctx.stroke()
        ctx.globalAlpha = 1
      }
      continue
    }
    const pos = messengerPos(m)
    // 拖尾:沿路往回取一点
    const tail = messengerPos({ ...m, dist: Math.max(0, m.dist - 14) })
    const grad = ctx.createLinearGradient(tail.x, tail.y, pos.x, pos.y)
    grad.addColorStop(0, 'rgba(126, 227, 240, 0)')
    grad.addColorStop(1, 'rgba(126, 227, 240, 0.85)')
    ctx.strokeStyle = grad
    ctx.lineWidth = 1.8
    ctx.beginPath()
    ctx.moveTo(tail.x, tail.y)
    ctx.lineTo(pos.x, pos.y)
    ctx.stroke()
    ctx.fillStyle = PALETTE.messenger
    ctx.shadowColor = PALETTE.messenger
    ctx.shadowBlur = 7
    ctx.beginPath()
    ctx.arc(pos.x, pos.y, 2.2, 0, Math.PI * 2)
    ctx.fill()
    ctx.shadowBlur = 0
  }

  // 引擎推进放在绘制后,保证 reduced-motion 下首帧即完整静态
  if (!reducedMotion && props.active) eng.tick(dt)

  // HUD 同步(节流)
  if (s.time - hudSyncAt > 120 || hudSyncAt === 0) {
    hudSyncAt = s.time
    litRooms.value = s.stats.litRooms
    delivered.value = s.stats.delivered
    doneSentences.value = s.stats.sentences
    memoryRooms.value = s.stats.memoryRooms
    phase.value = s.phase
    const ev = s.events[s.events.length - 1]
    if (ev) lastEvent.value = ev.text
    sentenceLines.value = s.sentences.map((line) => ({
      target: line.target,
      arrived: line.arrived.map((a) => a.text),
      done: line.completedAt >= 0,
    }))
  }
}

/** 画布坐标(逻辑 px)→ 命中的房间 key */
function hitTestRoom(cssX: number, cssY: number): string | null {
  const eng = engine
  if (!eng) return null
  for (const b of eng.state.buildings) {
    for (let i = 0; i < b.rooms.length; i++) {
      if (b.rooms[i] < 0 && b.rooms[i] !== -2) continue // 只响应已点亮(本次或记忆)
      const col = i % b.cols
      const row = Math.floor(i / b.cols)
      const wx = b.x + b.cell * (col + 0.85)
      const wy = b.y + b.cell * (row + 0.75)
      const size = b.cell * 0.52
      if (cssX >= wx - 2 && cssX <= wx + size + 2 && cssY >= wy - 2 && cssY <= wy + size + 2) {
        return `${b.id}:${i}`
      }
    }
  }
  return null
}

function onCanvasClick(ev: MouseEvent): void {
  const canvas = canvasRef.value
  if (!canvas) return
  const rect = canvas.getBoundingClientRect()
  const key = hitTestRoom(ev.clientX - rect.left, ev.clientY - rect.top)
  if (!key) {
    selectedRoom.value = null
    return
  }
  const meta = resolveRoomMeta(key)
  const memory = getMemoryEntries(memoryUserId.value).find((e) => e.key === key)
  selectedRoom.value = {
    key,
    theme: meta?.theme ?? '知识块',
    label: meta?.label ?? key,
    count: memory?.count ?? 1,
    firstAt: memory?.firstAt ?? Date.now(),
    lastAt: memory?.lastAt ?? Date.now(),
  }
}

function formatTime(ts: number): string {
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function loop(now: number): void {
  if (!props.active) return
  const dt = lastFrameAt ? now - lastFrameAt : 16
  lastFrameAt = now
  drawFrame(dt)
  rafId = window.requestAnimationFrame(loop)
}

watch(
  () => props.active,
  (active) => {
    if (reducedMotion) return
    if (active) {
      lastFrameAt = 0
      rafId = window.requestAnimationFrame(loop)
    } else {
      // 思考结束:把本次点亮的房间写回长期记忆
      flushSessionMemory()
      window.cancelAnimationFrame(rafId)
      drawFrame(0)
    }
  },
)

watch(
  () => props.sentences,
  () => {
    fitCanvas()
  },
)

onMounted(() => {
  fitCanvas()
  resizeObserver = new ResizeObserver(() => fitCanvas())
  if (wrapRef.value) resizeObserver.observe(wrapRef.value)
  if (!reducedMotion && props.active) rafId = window.requestAnimationFrame(loop)
})

onBeforeUnmount(() => {
  flushSessionMemory()
  window.cancelAnimationFrame(rafId)
  resizeObserver?.disconnect()
})

/** 当前句子的词汇槽位(已到词高亮,未到词虚线占位) */
const lexiconForSlots = computed(() => {
  const eng = engine
  if (!eng) return [] as string[]
  return eng.state.buildings.flatMap((b) => b.words)
})

function slotList(line: { target: string; arrived: string[] }): Array<{ text: string; hit: boolean }> {
  const tokens = tokenizeSentence(line.target, lexiconForSlots.value)
  return tokens.map((text, i) => ({ text, hit: i < line.arrived.length }))
}
</script>

<template>
  <div ref="wrapRef" class="thinking-city" :class="{ 'is-compact': compact }" role="img"
    :aria-label="`小菱的思考城市:已点亮 ${litRooms}/${totalRooms} 个知识房间,送达 ${delivered} 个词汇`">
    <canvas ref="canvasRef" class="thinking-city-canvas" aria-hidden="true" @click="onCanvasClick" />

    <!-- 积累徽章:已有历史沉淀时展示 -->
    <div v-if="memoryRooms > 0" class="memory-badge" :title="`小菱已经为你积累了 ${memoryRooms} 块知识,点击亮灯房间可查看`">
      <span class="memory-badge-icon" aria-hidden="true">◆</span>
      我的知识城市 · {{ memoryRooms }} 块
    </div>

    <!-- 知识卡片:点击亮灯房间弹出 -->
    <Transition name="room-card">
      <div v-if="selectedRoom" class="room-card" role="dialog" :aria-label="`知识卡片:${selectedRoom.label}`">
        <header class="room-card-head">
          <span class="room-card-theme">{{ selectedRoom.theme }}</span>
          <button class="room-card-close" type="button" aria-label="关闭" @click="selectedRoom = null">×</button>
        </header>
        <div class="room-card-label">{{ selectedRoom.label }}</div>
        <dl class="room-card-meta">
          <div><dt>点亮次数</dt><dd>{{ selectedRoom.count }} 次</dd></div>
          <div><dt>首次点亮</dt><dd>{{ formatTime(selectedRoom.firstAt) }}</dd></div>
          <div><dt>最近点亮</dt><dd>{{ formatTime(selectedRoom.lastAt) }}</dd></div>
        </dl>
      </div>
    </Transition>

    <div class="thinking-city-hud">
      <div class="hud-row hud-stats">
        <span class="hud-chip" title="本次思考点亮的房间数 / 全部房间">
          <i class="hud-dot hud-dot-lit" />本次点亮 {{ litRooms }}
        </span>
        <span class="hud-chip" title="历史积累的知识房间数">
          <i class="hud-dot hud-dot-memory" />已积累 {{ memoryRooms }}
        </span>
        <span class="hud-chip" title="信使已送达广场的词汇数">
          <i class="hud-dot hud-dot-road" />知识流 {{ delivered }}
        </span>
        <span class="hud-chip" title="已拼成的完整思路">
          <i class="hud-dot hud-dot-done" />思路 {{ doneSentences }}/{{ sentenceLines.length }}
        </span>
      </div>
      <div class="hud-row hud-phase">{{ phaseLabel }}<span v-if="lastEvent" class="hud-event">· {{ lastEvent }}</span></div>

      <div class="hud-sentences" aria-label="思路组装区">
        <div v-for="(line, i) in sentenceLines" :key="i" class="hud-sentence" :class="{ 'is-done': line.done }">
          <span
            v-for="(slot, j) in slotList(line)"
            :key="j"
            class="hud-word"
            :class="{ 'is-hit': slot.hit }"
          >{{ slot.hit ? slot.text : '· ·' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.thinking-city {
  position: relative;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid rgba(142, 136, 245, 0.35);
  background: #171332;
  box-shadow:
    inset 0 0 24px rgba(36, 29, 82, 0.6),
    0 4px 18px rgba(91, 88, 232, 0.18);
}

.thinking-city-canvas {
  display: block;
  width: 100%;
}

.thinking-city-hud {
  padding: 8px 12px 10px;
  background: linear-gradient(180deg, rgba(23, 19, 50, 0.2), rgba(23, 19, 50, 0.92));
  border-top: 1px solid rgba(142, 136, 245, 0.22);
  font-family: var(--font-mono);
}

.hud-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
}

.hud-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  letter-spacing: 0.02em;
  color: rgba(220, 218, 253, 0.88);
  background: rgba(142, 136, 245, 0.12);
  border: 1px solid rgba(142, 136, 245, 0.24);
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}

.hud-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.hud-dot-lit { background: #8e88f5; box-shadow: 0 0 5px #8e88f5; }
.hud-dot-memory { background: #e8e6fe; box-shadow: 0 0 5px #e8e6fe; }
.hud-dot-road { background: #7ee3f0; box-shadow: 0 0 5px #7ee3f0; }
.hud-dot-done { background: #d4a53a; box-shadow: 0 0 5px #d4a53a; }

/* 历史积累徽章(右上角) */
.memory-badge {
  position: absolute;
  top: 10px;
  right: 12px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-family: var(--font-mono);
  letter-spacing: 0.02em;
  color: #e8e6fe;
  background: rgba(36, 29, 82, 0.72);
  border: 1px solid rgba(232, 230, 254, 0.35);
  border-radius: 999px;
  padding: 3px 10px;
  backdrop-filter: blur(4px);
  pointer-events: none;
}

.memory-badge-icon {
  font-size: 8px;
  color: #d4a53a;
}

/* 知识卡片(点击亮灯房间弹出) */
.room-card {
  position: absolute;
  left: 12px;
  bottom: 12px;
  width: 200px;
  background: rgba(23, 19, 50, 0.95);
  border: 1px solid rgba(142, 136, 245, 0.5);
  border-radius: 10px;
  padding: 10px 12px 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(6px);
  font-family: var(--font-mono);
  z-index: 3;
}

.room-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.room-card-theme {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: #7ee3f0;
  text-transform: uppercase;
}

.room-card-close {
  border: none;
  background: transparent;
  color: rgba(183, 179, 251, 0.7);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}

.room-card-close:hover { color: #fff; }

.room-card-label {
  font-size: 13px;
  font-weight: 600;
  color: #efeefe;
  margin-bottom: 8px;
}

.room-card-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
}

.room-card-meta > div {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
}

.room-card-meta dt { color: rgba(183, 179, 251, 0.6); margin: 0; }
.room-card-meta dd { color: #dcdafd; margin: 0; }

.room-card-enter-active,
.room-card-leave-active { transition: opacity 0.22s ease, transform 0.22s cubic-bezier(0.16, 0.84, 0.44, 1); }
.room-card-enter-from,
.room-card-leave-to { opacity: 0; transform: translateY(6px) scale(0.97); }

.hud-phase {
  margin-top: 6px;
  font-size: 10.5px;
  color: rgba(126, 227, 240, 0.92);
}

.hud-event {
  color: rgba(183, 179, 251, 0.62);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.hud-sentences {
  margin-top: 7px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.hud-sentence {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  opacity: 0.55;
  transition: opacity 0.4s ease;
}

.hud-sentence.is-done {
  opacity: 1;
}

.hud-word {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px dashed rgba(142, 136, 245, 0.4);
  color: rgba(183, 179, 251, 0.5);
  transition: all 0.35s ease;
}

.hud-word.is-hit {
  border-style: solid;
  border-color: rgba(126, 227, 240, 0.75);
  color: #e8fbff;
  background: rgba(61, 188, 217, 0.18);
  box-shadow: 0 0 8px rgba(61, 188, 217, 0.35);
}

.is-compact .hud-phase { display: none; }
.is-compact .hud-sentences { max-height: 44px; overflow: hidden; }

@media (prefers-reduced-motion: reduce) {
  .hud-sentence,
  .hud-word,
  .room-card-enter-active,
  .room-card-leave-active { transition: none; }
}
</style>
