<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { AgentTeamDetail, AgentTeamMember, AgentTeamTask } from '@/api/agentTeams'

const props = withDefaults(defineProps<{
  teams: AgentTeamDetail[]
  forming?: boolean
  error?: string
}>(), { forming: false, error: '' })
const emit = defineEmits<{ 'open-detail': [team: AgentTeamDetail] }>()
const rootRef = ref<HTMLElement | null>(null)
const containerWidth = ref(600)
const zoom = ref(1)
const minZoom = 0.75
const maxZoom = 2
const labels: Record<string, string> = {
  created: '等待派发', draft: '正在组建', queued: '排队中', waiting_dependency: '等待前置任务',
  running: '执行中', verifying: '验证中', completed: '已完成', blocked: '已阻塞',
  failed: '执行失败', dead_letter: '投递终止', cancelled: '已取消', expired: '已过期', reclaimed: '已回收',
  stopped: '未确认完成',
}
const terminal = new Set(['completed', 'failed', 'blocked', 'cancelled', 'expired'])
function memberWork(team: AgentTeamDetail, member: AgentTeamMember) {
  const tasks = team.tasks.filter(task => task.member_id === member.member_id || task.member_key === member.member_key)
  const task: AgentTeamTask | undefined = tasks.find(item => item.status === 'running')
    ?? tasks.find(item => ['queued', 'waiting_dependency'].includes(item.status))
    ?? tasks.at(-1)
  const memberStatus = task?.status ?? member.status
  // 聚合终态只停止过期动画，不能据此补造成员完成或掩盖投递失败。
  const active = ['created', 'queued', 'waiting_dependency', 'running', 'verifying'].includes(memberStatus)
  const status = active && ['cancelled', 'expired'].includes(team.status)
    ? team.status
    : terminal.has(team.status) && active && memberStatus !== 'created'
      ? 'stopped' : memberStatus
  return { member, task, status, label: labels[status] ?? status }
}
const graphs = computed(() => props.teams.map(team => ({
  team,
  width: containerWidth.value,
  compact: containerWidth.value < 600,
  nodeWidth: containerWidth.value < 600
    ? Math.max(208, containerWidth.value - 32)
    : Math.max(268, (containerWidth.value - 64) / 2),
  nodes: team.members.map((member, index) => ({
    ...memberWork(team, member),
    x: containerWidth.value < 600
      ? 16
      : 16 + (index % 2) * (Math.max(268, (containerWidth.value - 64) / 2) + 32),
    y: 90 + Math.floor(index / (containerWidth.value < 600 ? 1 : 2)) * 100,
  })),
  height: 100 + Math.ceil(team.members.length / (containerWidth.value < 600 ? 1 : 2)) * 100,
})))
const memberCount = computed(() => props.teams.reduce((total, team) => total + team.members.length, 0))
const runningCount = computed(() => graphs.value.reduce((total, graph) => total + graph.nodes.filter(node => node.status === 'running').length, 0))
const short = (text: string, size = 18) => text.length > size ? `${text.slice(0, size)}…` : text
function resizeMap(entries: ResizeObserverEntry[]) {
  const width = entries[0]?.contentRect.width
  if (width && Number.isFinite(width)) containerWidth.value = Math.max(240, Math.round(width))
}
function zoomIn() { zoom.value = Math.min(maxZoom, Math.round((zoom.value + 0.25) * 100) / 100) }
function zoomOut() { zoom.value = Math.max(minZoom, Math.round((zoom.value - 0.25) * 100) / 100) }
function showOverview() {
  zoom.value = 1
  void nextTick(() => {
    rootRef.value?.querySelectorAll<HTMLElement>('.live-map-scroll').forEach(element => {
      element.scrollLeft = 0
      element.scrollTop = 0
    })
  })
}
watch(rootRef, (root, _previous, onCleanup) => {
  if (!root || typeof window === 'undefined' || !window.ResizeObserver) return
  const padding = getComputedStyle(root)
  const width = root.clientWidth
    - Number.parseFloat(padding.paddingLeft)
    - Number.parseFloat(padding.paddingRight)
  if (width > 0) containerWidth.value = Math.max(240, Math.round(width))
  const observer = new window.ResizeObserver(entries => {
    if (rootRef.value === root) resizeMap(entries)
  })
  onCleanup(() => observer.disconnect())
  observer.observe(root)
}, { flush: 'post' })
</script>

<template>
  <section v-if="teams.length || forming" ref="rootRef" class="agent-team-live" aria-label="实时团队星图">
    <header class="live-heading">
      <strong>团队实时星图</strong>
      <span v-if="forming" role="status">{{ teams.length ? '正在组建新团队 · 新成员以服务端账本确认为准' : '正在组建团队 · 等待服务端确认成员' }}</span>
      <span v-if="teams.length" role="status">{{ memberCount }} 个子 Agent · {{ runningCount }} 个执行中</span>
      <span v-else-if="!forming" role="status">等待团队账本更新</span>
      <div v-if="teams.length" class="live-controls" role="group" aria-label="星图缩放控制">
        <button type="button" aria-label="缩小星图" title="缩小星图" :disabled="zoom <= minZoom" @click="zoomOut">−</button>
        <span aria-live="polite">{{ Math.round(zoom * 100) }}%</span>
        <button type="button" aria-label="放大星图" title="放大星图" :disabled="zoom >= maxZoom" @click="zoomIn">+</button>
        <button type="button" aria-label="总览星图" title="适配并回到总览" @click="showOverview">总览</button>
      </div>
    </header>
    <p v-if="error" class="live-error" role="status">{{ error }}，保留上次确认的进度</p>
    <div v-for="graph in graphs" :key="graph.team.team_id" class="live-team">
      <div class="live-team-heading">
        <span>{{ graph.team.title }} · {{ labels[graph.team.status] ?? graph.team.status }}</span>
        <button type="button" @click="emit('open-detail', graph.team)">查看详情</button>
      </div>
      <div class="live-map-scroll" tabindex="0" :aria-label="`${graph.team.title}成员与当前任务`">
        <svg :viewBox="`0 0 ${graph.width} ${graph.height}`" :style="{ width: `${zoom * 100}%` }" class="live-map" role="img" :aria-label="`${graph.team.title}，${graph.nodes.length} 个子 Agent`">
          <g class="live-host"><circle :cx="graph.width / 2" cy="36" r="24" /><text :x="graph.width / 2" y="41" text-anchor="middle">小菱</text></g>
          <g v-for="node in graph.nodes" :key="node.member.member_id" :data-member-id="node.member.member_id" :data-status="node.status" :class="['live-node', `is-${node.status}`]">
            <title>{{ node.member.display_name }} · {{ node.label }} · {{ node.task?.title ?? '等待派发' }}</title>
            <path :d="`M${graph.width / 2} 60 C${graph.width / 2} ${node.y - 20}, ${node.x + graph.nodeWidth / 2} ${node.y - 20}, ${node.x + graph.nodeWidth / 2} ${node.y}`" class="live-edge" />
            <rect :x="node.x" :y="node.y" :width="graph.nodeWidth" height="80" rx="12" />
            <text :x="node.x + 16" :y="node.y + 20" class="live-name">{{ short(node.member.display_name, Math.floor((graph.nodeWidth - 32) / 12)) }}</text>
            <text :x="node.x + 16" :y="node.y + 40" class="live-status">{{ node.label }}</text>
            <text :x="node.x + 16" :y="node.y + 60" class="live-task">{{ short(node.task?.title ?? '等待派发', Math.floor((graph.nodeWidth - 32) / 12)) }}</text>
          </g>
        </svg>
      </div>
      <ul class="live-work-list" aria-label="子 Agent 当前工作">
        <li v-for="node in graph.nodes" :key="node.member.member_id">
          <b>{{ node.member.display_name }}</b><span>{{ node.label }} · {{ node.task?.title ?? '等待派发' }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.agent-team-live { margin: 12px 0; padding: 14px; border: 1px solid var(--el-border-color-light, #dddfe8); border-radius: 14px; background: var(--el-fill-color-blank, #fff); color: var(--el-text-color-primary, #303346); }
.live-heading, .live-team-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; font-size: 13px; }
.live-heading > span { font-size: 12px; color: var(--el-text-color-secondary, #626579); }
.live-controls { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; }
.live-controls button { min-width: 32px; min-height: 32px; padding: 0 8px; border: 1px solid var(--el-border-color-light, #dddfe8); border-radius: 6px; background: var(--el-fill-color-blank, #fff); color: var(--el-text-color-primary, #303346); cursor: pointer; }
.live-controls button:disabled { cursor: not-allowed; opacity: .45; }
.live-controls span { min-width: 42px; text-align: center; font-size: 12px; color: var(--el-text-color-secondary, #626579); }
.live-team { margin-top: 12px; }
.live-team-heading button { border: 0; background: transparent; color: var(--el-color-primary, #6658d9); cursor: pointer; min-height: 32px; }
.live-map-scroll { max-height: 340px; overflow: auto; overscroll-behavior: contain; }
.live-map { display: block; max-width: none; }
.live-host circle { fill: #eeeaff; stroke: #8673dd; }
.live-host text { fill: #514292; font-size: 13px; }
.live-node { --node-color: #808594; }
.live-node rect { fill: #f8f8fc; stroke: var(--node-color); }
.live-node circle { fill: var(--node-color); }
.live-edge { fill: none; stroke: var(--node-color); stroke-width: 1.4; opacity: .55; }
.live-name { fill: #303346; font-size: 12px; font-weight: 600; }
.live-status, .live-task { fill: #636879; font-size: 11px; }
.is-running { --node-color: #7561d2; }
.is-running .live-edge { stroke-dasharray: 6 5; animation: dispatch-flow 1.4s linear infinite; opacity: 1; }
.is-completed { --node-color: #41906b; }
.is-blocked, .is-failed, .is-dead_letter { --node-color: #be7854; }
.live-work-list { padding: 0; margin: 8px 0 0; list-style: none; max-height: 160px; overflow: auto; }
.live-work-list li { display: flex; flex-wrap: wrap; gap: 4px 10px; padding: 5px 0; font-size: 12px; overflow-wrap: anywhere; }
.live-work-list span { color: var(--el-text-color-secondary, #626579); }
.live-error { color: var(--el-color-warning-dark-2, #9d651d); font-size: 12px; }
@keyframes dispatch-flow { to { stroke-dashoffset: -22; } }
@media (prefers-reduced-motion: reduce) { .is-running .live-edge { animation: none; } }
</style>
