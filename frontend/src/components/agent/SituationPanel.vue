<script setup lang="ts">
import { computed } from 'vue'
import type { AgentSituationOut } from '@/types/agent'

interface Props {
  data: AgentSituationOut | null
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), { loading: false })

const safe = computed<AgentSituationOut>(() => props.data ?? {
  online: 0, working: 0, idle: 0, today_calls: 0,
  spectrum: [], hotspots: [],
})

const spectrumPath = computed(() => {
  const points = safe.value.spectrum
  if (!points.length) return ''
  const max = Math.max(1, ...points.map((p) => p.count))
  const w = 320
  const h = 40
  const step = w / Math.max(1, points.length - 1)
  return points
    .map((p, i) => {
      const x = i * step
      const y = h - (p.count / max) * h
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
})

const spectrumArea = computed(() => {
  const line = spectrumPath.value
  if (!line) return ''
  const points = safe.value.spectrum
  const w = 320
  const h = 40
  const step = w / Math.max(1, points.length - 1)
  const last = (points.length - 1) * step
  return `${line} L${last.toFixed(1)},${h} L0,${h} Z`
})

const lastBucket = computed(() => safe.value.spectrum.at(-1)?.bucket ?? '--:--')
const firstBucket = computed(() => safe.value.spectrum[0]?.bucket ?? '--:--')

const peakBucket = computed(() => {
  const sp = safe.value.spectrum
  if (!sp.length) return null
  return [...sp].sort((a, b) => b.count - a.count)[0]
})
</script>

<template>
  <section class="situation" :class="{ 'is-loading': loading }">
    <div class="head">
      <span class="prism-mark" aria-hidden="true"></span>
      <h2 class="title">态势感知</h2>
      <span class="hint">实时 · 自动同步</span>
    </div>

    <div class="metric-row">
      <div class="metric">
        <span class="metric-num">{{ safe.online }}</span>
        <span class="metric-label">在岗</span>
      </div>
      <div class="metric metric-busy">
        <span class="metric-num">{{ safe.working }}</span>
        <span class="metric-label">工作中</span>
      </div>
      <div class="metric metric-idle">
        <span class="metric-num">{{ safe.idle }}</span>
        <span class="metric-label">空闲</span>
      </div>
      <div class="metric">
        <span class="metric-num">{{ safe.today_calls }}</span>
        <span class="metric-label">今日调用</span>
      </div>
    </div>

    <div class="spectrum-block">
      <div class="spectrum-meta">
        <span>近 {{ safe.spectrum.length || 60 }} 分钟调用波形</span>
        <span class="font-mono">{{ firstBucket }} → {{ lastBucket }}</span>
      </div>
      <svg viewBox="0 0 320 40" preserveAspectRatio="none" class="spectrum-svg" aria-hidden="true">
        <defs>
          <linearGradient id="spec-grad" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" stop-color="#5B58E8" stop-opacity="0.35" />
            <stop offset="1" stop-color="#5B58E8" stop-opacity="0" />
          </linearGradient>
        </defs>
        <path v-if="spectrumArea" :d="spectrumArea" fill="url(#spec-grad)" />
        <path v-if="spectrumPath" :d="spectrumPath" fill="none" stroke="#5B58E8" stroke-width="1.5" />
        <text v-if="!spectrumPath" x="160" y="22" text-anchor="middle" font-size="11" fill="#9BA3B0">
          暂无近 1 小时数据
        </text>
      </svg>
      <div v-if="peakBucket && peakBucket.count > 0" class="spectrum-peak font-mono">
        峰值 {{ peakBucket.bucket }} · {{ peakBucket.count }} 次
      </div>
    </div>

    <div class="hotspot-block">
      <div class="hotspot-title">热点 Agent</div>
      <div v-if="safe.hotspots.length" class="hotspot-list">
        <div
          v-for="(h, idx) in safe.hotspots"
          :key="h.code"
          class="hotspot-item"
        >
          <span class="rank">#{{ idx + 1 }}</span>
          <span class="name">{{ h.name }}</span>
          <span class="count font-mono">{{ h.count }}</span>
        </div>
      </div>
      <div v-else class="hotspot-empty">今日尚无 Agent 被调用</div>
    </div>
  </section>
</template>

<style scoped lang="scss">
.situation {
  background:
    linear-gradient(135deg, #161A24 0%, #1F2330 62%, #252A37 100%);
  color: #fff;
  border-radius: 10px;
  padding: 22px 24px;
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 24px;
  align-items: stretch;
  position: relative;
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    inset: 0 0 auto;
    height: 3px;
    background: linear-gradient(90deg,
      #5B58E8, #3DBCD9, #5BB89A, #D9A857, #E27C4A, #DC4961, #9F7AEA, #5B58E8);
    opacity: 0.9;
    pointer-events: none;
  }

  & > * { position: relative; z-index: 1; }

  &.is-loading { opacity: 0.85; }
}

.head {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;

  .prism-mark {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    background: linear-gradient(135deg, #5B58E8, #3DBCD9 48%, #D9A857);
  }

  .title {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }

  .hint {
    margin-left: auto;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.55);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
}

.metric-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  align-content: start;
}

.metric {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;

  .metric-num {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: 0;
  }

  .metric-label {
    font-size: 11px;
    color: rgba(255, 255, 255, 0.6);
  }

  &.metric-busy .metric-num { color: #3DBCD9; }
  &.metric-idle .metric-num { color: #5BB89A; }
}

.spectrum-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;

  .spectrum-meta {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.55);
  }

  .spectrum-svg {
    width: 100%;
    height: 60px;
    flex: 1;
  }

  .spectrum-peak {
    font-size: 11px;
    color: rgba(255, 255, 255, 0.7);
  }
}

.hotspot-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.hotspot-title {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  letter-spacing: 0.05em;
}

.hotspot-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.hotspot-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.06);
  font-size: 12px;

  .rank {
    font-family: var(--font-mono);
    font-size: 10px;
    color: rgba(255, 255, 255, 0.5);
    width: 22px;
  }

  .name {
    color: #fff;
    font-weight: 500;
  }

  .count {
    color: #3DBCD9;
    font-weight: 600;
  }
}

.hotspot-empty {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
  padding: 8px 0;
}

@media (max-width: 960px) {
  .situation {
    grid-template-columns: 1fr;
  }
  .metric-row { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
</style>
