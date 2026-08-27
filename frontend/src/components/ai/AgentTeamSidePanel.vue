<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { Close, CircleCheck, Loading, WarningFilled } from "@element-plus/icons-vue"
import AgentTeamMemberBadge from "./AgentTeamMemberBadge.vue"
import AgentMemberWorkCard from "./AgentMemberWorkCard.vue"
import { getAgentTeam, type AgentTeamDetail, type AgentTeamEvent, type AgentTeamMember, type AgentTeamTask } from "@/api/agentTeams"

const props = defineProps<{
  teamId: number | null
}>()

const emit = defineEmits<{
  close: []
  "ask-member": [payload: { teamId: number; name: string; address: string }]
}>()

const team = ref<AgentTeamDetail | null>(null)
const loading = ref(false)
const error = ref("")
const activeTab = ref<"overview" | "members" | "tasks" | "events">("overview")
const now = ref(Date.now())
let pollTimer: ReturnType<typeof setTimeout> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let pollGeneration = 0

function stopPolling(): void {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = undefined }
}

function schedulePolling(teamId: number, generation: number): void {
  stopPolling()
  if (generation !== pollGeneration || props.teamId !== teamId || isTerminal.value) return
  pollTimer = setTimeout(() => {
    pollTimer = undefined
    void fetchTeam(teamId, generation, false)
  }, 5000)
}

async function fetchTeam(teamId: number, generation: number, initial: boolean): Promise<void> {
  if (initial) {
    loading.value = true
    error.value = ""
  }
  try {
    const detail = await getAgentTeam(teamId)
    if (generation !== pollGeneration || props.teamId !== teamId) return
    team.value = detail
    error.value = ""
  } catch {
    if (generation !== pollGeneration || props.teamId !== teamId) return
    if (initial) error.value = "加载团队详情失败"
  } finally {
    if (generation === pollGeneration && props.teamId === teamId) {
      if (initial) loading.value = false
      schedulePolling(teamId, generation)
    }
  }
}

function loadTeam(teamId: number | null): void {
  pollGeneration += 1
  stopPolling()
  team.value = null
  error.value = ""
  loading.value = false
  activeTab.value = "overview"
  if (!teamId) return
  void fetchTeam(teamId, pollGeneration, true)
}

const isTerminal = computed(() => ["completed", "failed", "cancelled", "expired"].includes(team.value?.status ?? ""))

watch(() => props.teamId, loadTeam, { immediate: true })
watch(isTerminal, (t) => { if (t) stopPolling() })

onMounted(() => {
  // 秒级节拍驱动成员计时文案;5s 粒度会以 5 为步长跳变
  clockTimer = setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  pollGeneration += 1
  stopPolling()
  if (clockTimer) clearInterval(clockTimer)
})

const members = computed<AgentTeamMember[]>(() => team.value?.members ?? [])
const tasks = computed<AgentTeamTask[]>(() => team.value?.tasks ?? [])
const events = computed<AgentTeamEvent[]>(() => team.value?.events ?? [])

const workingDuration = computed(() => {
  if (!team.value) return ""
  const start = team.value.started_at ? new Date(team.value.started_at).getTime() : new Date(team.value.created_at ?? "").getTime()
  const end = team.value.completed_at ? new Date(team.value.completed_at).getTime() : now.value
  const diff = Math.max(0, Math.floor((end - start) / 1000))
  const h = Math.floor(diff / 3600)
  const m = Math.floor((diff % 3600) / 60)
  const s = diff % 60
  if (h > 0) return `${h}小时${m}分${s}秒`
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
})

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿", queued: "排队中", running: "运行中", verifying: "验证中",
  completed: "已完成", failed: "失败", cancelled: "已取消", expired: "已过期",
  waiting_dependency: "等待依赖", blocked: "已阻塞", dead_letter: "死信",
}

function taskStatusIcon(s: string) {
  if (s === "completed") return "done"
  if (s === "running") return "running"
  if (["failed", "dead_letter"].includes(s)) return "fail"
  return "pending"
}

function eventTime(e: AgentTeamEvent): string {
  if (!e.created_at) return ""
  return new Date(e.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}

function askMember(member: AgentTeamMember): void {
  const teamId = team.value?.team_id ?? props.teamId
  if (!teamId) return
  emit("ask-member", { teamId, name: member.display_name, address: member.address })
}
</script>

<template>
  <Teleport to="body">
    <Transition name="panel-slide">
      <div v-if="teamId" class="team-side-panel-overlay" @click.self="emit('close')">
        <div
          class="team-side-panel"
          role="dialog"
          aria-modal="true"
          :aria-label="team?.title || '子Agent团队详情'"
        >
          <header class="panel-header">
            <h3 class="panel-title">{{ team?.title || "子Agent团队详情" }}</h3>
            <button
              class="panel-close"
              type="button"
              aria-label="关闭团队详情"
              title="关闭团队详情"
              @click="emit('close')"
            >
              <el-icon><Close /></el-icon>
            </button>
          </header>

          <div v-if="error" class="panel-error">{{ error }}</div>
          <div v-else-if="!team && loading" class="panel-loading">加载中...</div>

          <template v-else-if="team">
            <div class="panel-meta">
              <span class="meta-status" :class="`st-${team.status}`">{{ STATUS_LABELS[team.status] ?? team.status }}</span>
              <span class="meta-duration">已运行 {{ workingDuration }}</span>
            </div>

            <nav class="panel-tabs">
              <button v-for="tab in ([{ key: 'overview', label: '概览' }, { key: 'members', label: '成员' }, { key: 'tasks', label: '任务' }, { key: 'events', label: '事件' }] as const)"
                :key="tab.key" class="tab-btn" :class="{ active: activeTab === tab.key }"
                @click="activeTab = tab.key">{{ tab.label }}</button>
            </nav>

            <div class="panel-body">
              <div v-if="activeTab === 'overview'" class="tab-overview">
                <div v-if="team.objective" class="overview-section">
                  <div class="section-label">目标</div>
                  <p class="section-text">{{ team.objective }}</p>
                </div>
                <div class="overview-section">
                  <div class="section-label">成员 ({{ members.length }})</div>
                  <div class="overview-badges">
                    <AgentTeamMemberBadge v-for="m in members" :key="m.member_id" :name="m.display_name" :role="m.role" :status="m.status" :address="m.address" :interactive="false" />
                  </div>
                </div>
                <div class="overview-section">
                  <div class="section-label">任务进度</div>
                  <div class="progress-bar">
                    <div class="progress-done" :style="{ width: `${team.counts?.total ? (team.counts.completed / team.counts.total * 100) : 0}%` }" />
                  </div>
                  <div class="progress-text">
                    {{ team.counts?.completed ?? 0 }}/{{ team.counts?.total ?? tasks.length }} 已完成
                    <span v-if="team.counts?.running" class="p-run"> · {{ team.counts.running }} 运行中</span>
                    <span v-if="team.counts?.failed" class="p-fail"> · {{ team.counts.failed }} 失败</span>
                  </div>
                </div>
              </div>

              <div v-if="activeTab === 'members'" class="tab-members">
                <AgentMemberWorkCard
                  v-for="m in members"
                  :key="m.member_id"
                  :member="m"
                  :tasks="tasks"
                  :events="events"
                  :team-started-at="team?.started_at ?? null"
                  show-ask
                  :log-limit="5"
                  @ask="askMember"
                />
                <div v-if="!members.length" class="empty-tab">暂无成员数据</div>
              </div>

              <div v-if="activeTab === 'tasks'" class="tab-tasks">
                <div v-for="(task, i) in tasks" :key="task.task_id" class="task-row">
                  <span class="task-idx">{{ i + 1 }}</span>
                  <span class="task-icon" :class="`icon-${taskStatusIcon(task.status)}`">
                    <el-icon v-if="task.status === 'completed'" :size="14"><CircleCheck /></el-icon>
                    <el-icon v-else-if="task.status === 'running'" :size="14"><Loading class="spin-icon" /></el-icon>
                    <el-icon v-else-if="['failed', 'dead_letter'].includes(task.status)" :size="14"><WarningFilled /></el-icon>
                    <span v-else class="dot-pending" />
                  </span>
                  <div class="task-info">
                    <span class="task-title">{{ task.title }}</span>
                    <span v-if="task.depends_on?.length" class="task-deps">依赖: {{ task.depends_on.join(", ") }}</span>
                  </div>
                  <span class="task-status" :class="`st-${task.status}`">{{ STATUS_LABELS[task.status] ?? task.status }}</span>
                </div>
                <div v-if="!tasks.length" class="empty-tab">暂无任务数据</div>
              </div>

              <div v-if="activeTab === 'events'" class="tab-events">
                <div v-for="e in events" :key="e.event_id" class="event-row">
                  <span class="event-time">{{ eventTime(e) }}</span>
                  <span class="event-type">{{ e.event_type }}</span>
                  <span v-if="e.from_status || e.to_status" class="event-trans">
                    {{ e.from_status }} → {{ e.to_status }}
                  </span>
                </div>
                <div v-if="!events.length" class="empty-tab">暂无事件数据</div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.team-side-panel-overlay {
  position: fixed; inset: 0; z-index: 3200;
  background: rgba(0,0,0,0.18);
  display: flex; justify-content: flex-end;
}
.team-side-panel {
  width: 400px; max-width: 90vw; height: 100vh;
  background: #fff; box-shadow: -4px 0 24px rgba(0,0,0,0.12);
  display: flex; flex-direction: column; overflow: hidden;
}
.panel-slide-enter-active, .panel-slide-leave-active { transition: all 0.25s ease; }
.panel-slide-enter-from .team-side-panel, .panel-slide-leave-to .team-side-panel { transform: translateX(100%); }
.panel-slide-enter-from, .panel-slide-leave-to { opacity: 0; }

.panel-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid #e5e7eb; flex-shrink: 0; }
.panel-title { font-size: 15px; font-weight: 600; color: #1f2937; margin: 0; }
.panel-close { background: none; border: 0; cursor: pointer; padding: 4px; border-radius: 6px; color: #6b7280; display: flex; }
.panel-close:hover { background: #f3f4f6; }

.panel-error { padding: 16px; color: #dc2626; font-size: 13px; }
.panel-loading { padding: 24px; text-align: center; color: #9ca3af; font-size: 13px; }

.panel-meta { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid #f3f4f6; flex-shrink: 0; }
.meta-status { font-size: 12px; padding: 2px 8px; border-radius: 10px; font-weight: 500; }
.st-running { background: #dbeafe; color: #1d4ed8; }
.st-completed { background: #dcfce7; color: #15803d; }
.st-failed { background: #fee2e2; color: #dc2626; }
.st-queued { background: #f3f4f6; color: #6b7280; }
.st-verifying { background: #fef3c7; color: #b45309; }
.st-cancelled { background: #f3f4f6; color: #9ca3af; }
.st-expired { background: #f3f4f6; color: #9ca3af; }
.meta-duration { font-size: 12px; color: #9ca3af; }

.panel-tabs { display: flex; gap: 0; border-bottom: 1px solid #e5e7eb; flex-shrink: 0; padding: 0 16px; }
.tab-btn { flex: 1; padding: 10px 0; font-size: 13px; font-weight: 500; color: #6b7280; background: none; border: 0; border-bottom: 2px solid transparent; cursor: pointer; transition: all 0.15s; }
.tab-btn.active { color: #2563eb; border-bottom-color: #2563eb; }
.tab-btn:hover:not(.active) { color: #374151; }

.panel-body { flex: 1; overflow-y: auto; padding: 14px 16px; }

.overview-section { margin-bottom: 16px; }
.section-label { font-size: 12px; font-weight: 600; color: #6b7280; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.03em; }
.section-text { font-size: 13px; color: #374151; line-height: 1.5; margin: 0; }
.overview-badges { display: flex; flex-wrap: wrap; gap: 6px; }

.progress-bar { height: 6px; background: #f3f4f6; border-radius: 3px; overflow: hidden; margin-bottom: 6px; }
.progress-done { height: 100%; background: #22c55e; border-radius: 3px; transition: width 0.3s ease; }
.progress-text { font-size: 12px; color: #6b7280; }
.p-run { color: #2563eb; }
.p-fail { color: #dc2626; }

.tab-members { display: grid; gap: 10px; }

.task-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid #f9fafb; }
.task-idx { font-size: 12px; color: #9ca3af; width: 18px; text-align: right; flex-shrink: 0; }
.task-icon { flex-shrink: 0; display: flex; align-items: center; }
.icon-done { color: #22c55e; }
.icon-running { color: #3b82f6; }
.icon-fail { color: #ef4444; }
.dot-pending { width: 8px; height: 8px; border-radius: 50%; background: #d1d5db; }
.spin-icon { animation: spin 1.2s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.task-info { flex: 1; min-width: 0; }
.task-title { font-size: 13px; color: #1f2937; display: block; }
.task-deps { font-size: 11px; color: #9ca3af; display: block; }
.task-status { font-size: 11px; padding: 1px 6px; border-radius: 8px; white-space: nowrap; flex-shrink: 0; }

.event-row { display: flex; align-items: baseline; gap: 8px; padding: 5px 0; font-size: 12px; border-bottom: 1px solid #f9fafb; }
.event-time { color: #9ca3af; flex-shrink: 0; font-family: monospace; }
.event-type { color: #374151; font-weight: 500; }
.event-trans { color: #6b7280; }

.empty-tab { padding: 24px; text-align: center; color: #9ca3af; font-size: 13px; }
</style>
