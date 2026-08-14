<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { CircleCheck, Loading, WarningFilled, Timer } from "@element-plus/icons-vue"
import AgentTeamMemberBadge from "./AgentTeamMemberBadge.vue"
import type { AgentTeamDetail, AgentTeamEvent, AgentTeamMember, AgentTeamTask } from "@/api/agentTeams"

const props = defineProps<{
  team: AgentTeamDetail | null
  loading?: boolean
}>()

const emit = defineEmits<{
  "open-panel": [teamId: number]
}>()

const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined

onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 5000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })

const members = computed<AgentTeamMember[]>(() => props.team?.members ?? [])
const tasks = computed<AgentTeamTask[]>(() => props.team?.tasks ?? [])
const events = computed<AgentTeamEvent[]>(() => props.team?.events ?? [])

const counts = computed(() => {
  if (props.team?.counts) return props.team.counts
  const t = tasks.value
  return {
    total: t.length,
    completed: t.filter((x) => x.status === "completed").length,
    running: t.filter((x) => x.status === "running").length,
    queued: t.filter((x) => ["queued", "waiting_dependency"].includes(x.status)).length,
    failed: t.filter((x) => ["failed", "dead_letter", "expired"].includes(x.status)).length,
    blocked: t.filter((x) => x.status === "blocked").length,
  }
})

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿", queued: "排队中", running: "运行中", verifying: "验证中",
  completed: "已完成", failed: "失败", cancelled: "已取消", expired: "已过期",
}

const statusLabel = computed(() => STATUS_LABELS[props.team?.status ?? ""] ?? props.team?.status ?? "")
const isRunning = computed(() => ["running", "queued", "verifying"].includes(props.team?.status ?? ""))
const teamCardLabel = computed(() => (
  members.value.length
    ? `已创建 ${members.value.length} 个子Agent`
    : '子Agent创建中'
))

const workingDuration = computed(() => {
  if (!props.team) return ""
  const start = props.team.started_at ? new Date(props.team.started_at).getTime() : new Date(props.team.created_at ?? "").getTime()
  const end = props.team.completed_at ? new Date(props.team.completed_at).getTime() : now.value
  const diff = Math.max(0, Math.floor((end - start) / 1000))
  const m = Math.floor(diff / 60)
  const s = diff % 60
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
})

const MEMBER_STATUS_TEXT: Record<string, string> = {
  created: "排队中",
  queued: "排队中",
  running: "已开始工作",
  completed: "已完成",
  failed: "失败",
  reclaimed: "已回收",
}

const EVENT_STATUS_TEXT: Record<string, string> = {
  created: "排队中",
  queued: "排队中",
  waiting_dependency: "排队中",
  running: "已开始工作",
  verifying: "工作中",
  completed: "已完成",
  failed: "失败",
  dead_letter: "失败",
  expired: "失败",
  blocked: "被阻塞",
  cancelled: "已取消",
  reclaimed: "已回收",
}

const EVENT_ACTION_TEXT: Record<string, string> = {
  "task.claimed": "已开始工作",
  "task.completed": "已完成",
  "task.failed": "失败",
  "task.dead_letter": "失败",
  "task.expired": "失败",
  "task.queued": "排队",
  "task.retry_queued": "重新排队",
  "task.blocked": "被阻塞",
  "task.cancelled": "被取消",
  "team.created": "已创建",
  "team.status_changed": "已更新",
  "team.cancelled": "已取消",
  "team.retry_requested": "重新排队",
  "team.archived": "已归档",
}

function secondsBetween(start: string | null | undefined, end: number): number {
  if (!start) return 0
  const value = new Date(start).getTime()
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.floor((end - value) / 1000))
}

function memberStatusText(member: AgentTeamMember): string {
  if (member.status === "running") {
    const seconds = secondsBetween(member.started_at ?? props.team?.started_at, now.value)
    if (seconds <= 0) return "已开始工作"
    const minutes = Math.floor(seconds / 60)
    return minutes > 0 ? `工作中 ${minutes}分${seconds % 60}秒` : `工作中 ${seconds}秒`
  }
  return MEMBER_STATUS_TEXT[member.status] ?? member.status
}

const memberStatusSummary = computed(() => {
  if (!members.value.length) return "暂无成员状态"
  const summary = members.value
    .map((member) => `${member.display_name || member.member_key} ${memberStatusText(member)}`)
    .join(" · ")
  return `成员状态：${summary}`
})

const memberById = computed(() => {
  const map = new Map<number, AgentTeamMember>()
  for (const member of members.value) map.set(member.member_id, member)
  return map
})

function eventAction(event: AgentTeamEvent): string {
  const status = event.to_status || event.from_status || ""
  return EVENT_ACTION_TEXT[event.event_type] ?? EVENT_STATUS_TEXT[status] ?? event.event_type
}

function eventText(event: AgentTeamEvent): string {
  const action = eventAction(event)
  const member = event.member_id === null || event.member_id === undefined
    ? undefined
    : memberById.value.get(event.member_id)
  const name = member?.display_name || member?.member_key || ""
  return name ? `${name} ${action}` : action
}

const latestEvents = computed(() => {
  const list = [...events.value]
  list.sort((a, b) => {
    const aTime = a.created_at ? new Date(a.created_at).getTime() : 0
    const bTime = b.created_at ? new Date(b.created_at).getTime() : 0
    return (Number.isFinite(aTime) ? aTime : 0) - (Number.isFinite(bTime) ? bTime : 0)
  })
  return list.slice(-3)
})

function openPanel(): void {
  if (props.team) emit("open-panel", props.team.team_id)
}
</script>

<template>
  <div v-if="team" class="team-card" :class="`is-${team.status}`" @click="openPanel">
    <div class="team-card-header">
      <div class="team-card-icon">
        <el-icon v-if="isRunning" :size="16"><Loading class="spin-icon" /></el-icon>
        <el-icon v-else-if="team.status === 'completed'" :size="16" color="#4fb87a"><CircleCheck /></el-icon>
        <el-icon v-else-if="team.status === 'failed'" :size="16" color="#dc4961"><WarningFilled /></el-icon>
        <el-icon v-else :size="16" color="#9ba3b0"><Timer /></el-icon>
      </div>
      <div class="team-card-info">
        <span class="team-card-label">{{ teamCardLabel }}</span>
        <span class="team-card-title">{{ team.title || team.objective || "团队协作任务" }}</span>
      </div>
      <span class="team-card-status" :class="`st-${team.status}`">{{ statusLabel }}</span>
    </div>

    <div v-if="members.length" class="team-card-members">
      <div v-for="member in members" :key="member.member_id" class="team-card-member">
        <AgentTeamMemberBadge
          :name="member.display_name"
          :role="member.role"
          :status="member.status"
          :address="member.address"
          @click="openPanel"
        />
        <span class="team-card-member-status">{{ memberStatusText(member) }}</span>
      </div>
    </div>

    <div class="team-card-activity">
      <template v-if="latestEvents.length">
        <div v-for="event in latestEvents" :key="event.event_id" class="team-card-event">
          <span class="team-card-event-text">{{ eventText(event) }}</span>
        </div>
      </template>
      <div v-else class="team-card-event-summary">{{ memberStatusSummary }}</div>
    </div>

    <div class="team-card-footer">
      <span class="team-card-stats">
        <span v-if="counts.completed" class="stat stat-done">{{ counts.completed }}/{{ counts.total }} 已完成</span>
        <span v-if="counts.running" class="stat stat-run">{{ counts.running }} 运行中</span>
        <span v-if="counts.queued" class="stat stat-queue">{{ counts.queued }} 排队中</span>
        <span v-if="counts.failed" class="stat stat-fail">{{ counts.failed }} 失败</span>
      </span>
      <span class="team-card-duration">{{ isRunning ? "已运行" : "耗时" }} {{ workingDuration }}</span>
    </div>

    <div v-if="loading" class="team-card-loading">
      <span class="loading-dot" /><span class="loading-dot" /><span class="loading-dot" />
    </div>
  </div>
</template>

<style scoped>
.team-card {
  margin: 8px 0;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  background: #fafbfc;
  cursor: pointer;
  transition: all 0.15s ease;
  max-width: 560px;
}
.team-card:hover { border-color: var(--brand-300, #8e88f5); background: var(--brand-50, #EFEEFE); }
.team-card.is-running { border-left: 3px solid var(--brand-500, #5b58e8); }
.team-card.is-completed { border-left: 3px solid var(--color-success, #4fb87a); }
.team-card.is-failed { border-left: 3px solid var(--color-danger, #dc4961); }
.team-card.is-queued { border-left: 3px solid var(--gray-400, #9ba3b0); }

.team-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.team-card-icon { flex-shrink: 0; display: flex; align-items: center; }
.spin-icon { animation: spin 1.2s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.team-card-info { flex: 1; min-width: 0; }
.team-card-label { font-size: 12px; color: #6b7280; display: block; }
.team-card-title { font-size: 13px; font-weight: 600; color: #1f2937; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }

.team-card-status {
  font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500; white-space: nowrap; flex-shrink: 0;
}
.st-running { background: var(--brand-50, #EFEEFE); color: var(--brand-600, #4a46d4); }
.st-completed { background: var(--color-success-light, #e5f4ec); color: var(--color-success, #4fb87a); }
.st-failed { background: var(--color-danger-light, #fceaee); color: var(--color-danger, #dc4961); }
.st-queued { background: var(--gray-100, #eef0f4); color: var(--gray-500, #6e7689); }
.st-verifying { background: var(--sev-medium-bg, #faf1df); color: var(--sev-medium, #d9a857); }
.st-cancelled { background: var(--gray-100, #eef0f4); color: var(--gray-400, #9ba3b0); }
.st-expired { background: var(--gray-100, #eef0f4); color: var(--gray-400, #9ba3b0); }

.team-card-members { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.team-card-member { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; }
.team-card-member-status { font-size: 11px; color: #6b7280; line-height: 1.2; white-space: nowrap; }

.team-card-activity {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 8px;
  padding: 7px 8px;
  border-radius: 8px;
  background: #f3f4f6;
}
.team-card-event { font-size: 12px; color: #4b5563; line-height: 1.4; }
.team-card-event-text { word-break: break-word; }
.team-card-event-summary { font-size: 12px; color: #6b7280; }

.team-card-footer { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: #6b7280; }
.team-card-stats { display: flex; gap: 10px; }
.stat { white-space: nowrap; }
.stat-done { color: var(--color-success, #4fb87a); }
.stat-run { color: var(--brand-600, #4a46d4); }
.stat-queue { color: var(--gray-400, #9ba3b0); }
.stat-fail { color: var(--color-danger, #dc4961); }
.team-card-duration { color: var(--gray-400, #9ba3b0); white-space: nowrap; }

.team-card-loading { display: flex; gap: 4px; justify-content: center; padding-top: 6px; }
.loading-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--brand-300, #8e88f5); animation: load-blink 1s ease-in-out infinite; }
.loading-dot:nth-child(2) { animation-delay: 0.15s; }
.loading-dot:nth-child(3) { animation-delay: 0.3s; }
@keyframes load-blink { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }
</style>
