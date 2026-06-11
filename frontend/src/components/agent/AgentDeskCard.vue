<script setup lang="ts">
import { computed, ref } from 'vue'
import dayjs from 'dayjs'
import AgentAvatar from './AgentAvatar.vue'
import type { AgentRuntimeOut, AgentStatus } from '@/types/agent'
import { useTilt } from '@/utils/tilt'

interface Props {
  agent: AgentRuntimeOut
}

const props = defineProps<Props>()

const emit = defineEmits<{
  select: [code: string]
  invoke: [code: string]
}>()

const statusLabel: Record<AgentStatus, string> = {
  idle: '空闲',
  thinking: '思考中',
  working: '工作中',
  blocked: '等待回填',
  error: '调用失败',
  offline: '离线',
}

const lastCalled = computed(() => {
  if (!props.agent.last_called_at) return '尚未调用'
  return dayjs(props.agent.last_called_at).fromNow?.()
    ?? dayjs(props.agent.last_called_at).format('MM-DD HH:mm')
})

const successRate = computed(() => {
  const total = props.agent.call_count
  if (!total) return null
  return Math.round((props.agent.success_count / total) * 100)
})

const categoryLabel = computed(() => {
  const map: Record<string, string> = {
    meta: '主控',
    frontline: '前台',
    analyzer: '分析',
    reviewer: '审查',
    manager: '管理',
    orchestrator: '编排',
    analytics: '分析',
    output: '产出',
    general: '通用',
  }
  return map[props.agent.category] ?? props.agent.category
})

const cardRef = ref<HTMLElement | null>(null)
useTilt(cardRef, { max: 7, scale: 1.015 })
</script>

<template>
  <article
    ref="cardRef"
    class="desk-card"
    :style="{ '--accent': agent.color }"
    role="article"
    :aria-label="`Agent ${agent.name}, 状态 ${statusLabel[agent.status]}`"
    @click="emit('select', agent.code)"
  >
    <header class="card-head">
      <AgentAvatar
        :code="agent.code"
        :color="agent.color"
        :status="agent.status"
        :size="56"
        :label="agent.name"
      />
      <div class="head-meta">
        <div class="card-title">{{ agent.name }}</div>
        <code class="card-code">{{ agent.code }}</code>
      </div>
      <span class="status-pill" :class="`status-${agent.status}`">
        {{ statusLabel[agent.status] }}
      </span>
    </header>

    <p class="card-desc" :title="agent.description">{{ agent.description }}</p>

    <div class="card-tags">
      <span class="cat-tag">{{ categoryLabel }}</span>
      <span
        v-for="skill in agent.skills.slice(0, 3)"
        :key="skill"
        class="skill-tag"
      >
        {{ skill }}
      </span>
      <span v-if="agent.skills.length > 3" class="skill-tag muted">
        +{{ agent.skills.length - 3 }}
      </span>
    </div>

    <footer class="card-foot">
      <div class="stat">
        <span class="stat-val">{{ agent.call_count }}</span>
        <span class="stat-label">调用</span>
      </div>
      <div class="stat stat-ok">
        <span class="stat-val">{{ agent.success_count }}</span>
        <span class="stat-label">成功</span>
      </div>
      <div class="stat stat-err">
        <span class="stat-val">{{ agent.failed_count }}</span>
        <span class="stat-label">失败</span>
      </div>
      <div class="stat stat-rate">
        <span class="stat-val">{{ successRate !== null ? `${successRate}%` : '—' }}</span>
        <span class="stat-label">成功率</span>
      </div>
    </footer>

    <div class="card-last font-mono">最近 · {{ lastCalled }}</div>
  </article>
</template>

<style scoped lang="scss">
.desk-card {
  background: var(--surface-1);
  border: var(--hairline);
  border-radius: 10px;
  padding: 16px 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
  transition: all 0.18s ease;
  cursor: pointer;
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: 10px;
    pointer-events: none;
    background: linear-gradient(180deg, var(--accent, #5B58E8) 0%, transparent 6%);
    opacity: 0.7;
  }

  &:hover {
    border-color: var(--accent);
    box-shadow: var(--panel-shadow);
    transform: translateY(-1px);
  }
}

.card-head {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 12px;
}

.head-meta {
  min-width: 0;
}

.card-title {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--gray-900);
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-code {
  display: inline-block;
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--gray-500);
  background: var(--gray-50);
  padding: 1px 6px;
  border-radius: 4px;
}

.status-pill {
  font-size: 10.5px;
  padding: 3px 8px;
  border-radius: 999px;
  font-weight: 500;
  letter-spacing: 0.04em;

  &.status-idle    { background: var(--gray-100); color: var(--gray-600); }
  &.status-thinking{ background: #EEEEFE; color: #5B58E8; }
  &.status-working { background: #E1F5F9; color: #2A9D8F; }
  &.status-blocked { background: #FFF1D6; color: #B68039; }
  &.status-error   { background: #FDE5E9; color: #B83545; }
  &.status-offline { background: #F2F3F5; color: var(--gray-500); }
}

.card-desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--gray-700);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.cat-tag {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.04em;
}

.skill-tag {
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--gray-50);
  color: var(--gray-700);

  &.muted { color: var(--gray-500); }
}

.card-foot {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
  padding-top: 10px;
  border-top: 1px dashed var(--gray-100);
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;

  .stat-val {
    font-family: var(--font-display, inherit);
    font-size: 14.5px;
    font-weight: 600;
    color: var(--gray-900);
  }

  .stat-label {
    font-size: 10px;
    color: var(--gray-500);
  }

  &.stat-ok .stat-val   { color: #4FB87A; }
  &.stat-err .stat-val  { color: #DC4961; }
  &.stat-rate .stat-val { color: var(--accent); }
}

.card-last {
  font-size: 10.5px;
  color: var(--gray-400);
  text-align: right;
}
</style>
