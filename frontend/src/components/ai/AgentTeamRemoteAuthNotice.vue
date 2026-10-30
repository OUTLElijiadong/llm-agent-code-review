<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { AgentTeamTask } from '@/api/agentTeams'
import { useUserStore } from '@/stores/user'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import { collectRemoteAuthIssues } from '@/utils/remoteTarget'

/**
 * 小菱团队里“远程目标未授权”的提示卡。
 *
 * 远程目标只认“远程网站评估”里已由另一账号批准的委托；团队任务因此被拦下时
 * （服务端错误码 remote_target_authorization_required，或监督确认后仍含远程目标），
 * 在这里告诉用户去哪里申请，而不是只剩一个通用的“确认高风险重试”。
 * 卡片只展示脱敏后的目标来源（协议 + 主机），不展示路径、查询串或错误详情。
 */
const props = defineProps<{ tasks: readonly AgentTeamTask[] }>()

const router = useRouter()
const userStore = useUserStore()

const issues = computed(() => collectRemoteAuthIssues(props.tasks))
const canApply = computed(() => isNavigationPathAllowed(router, '/remote-pentests/start', userStore))
const canViewExisting = computed(() => isNavigationPathAllowed(router, '/remote-pentests', userStore))

function applyPath(origin: string): string {
  return origin
    ? `/remote-pentests/start?target_url=${encodeURIComponent(origin)}`
    : '/remote-pentests/start'
}

function goApply(origin: string): void {
  void router.push(applyPath(origin))
}

function goExisting(): void {
  void router.push('/remote-pentests')
}
</script>

<template>
  <section
    v-for="issue in issues"
    :key="issue.origin || 'unknown-target'"
    class="remote-auth-notice"
    role="alert"
    aria-label="目标未授权"
  >
    <strong class="remote-auth-title">目标未授权</strong>
    <p class="remote-auth-text">该远程目标尚未获得独立授权，请先提交远程网站评估并由另一位审批人批准</p>
    <p v-if="issue.origin" class="remote-auth-origin" :title="issue.origin">{{ issue.origin }}</p>
    <p v-if="issue.taskTitles.length" class="remote-auth-tasks">
      受影响的任务：{{ issue.taskTitles.join('、') }}
    </p>
    <p v-if="!issue.authorizable" class="remote-auth-hint">
      远程网站评估仅支持 HTTPS 默认端口的域名站点根地址，该目标无法通过它授权；请改用符合要求的站点地址。
    </p>
    <p v-else class="remote-auth-hint">批准后回到这里点“重试失败”即可继续。</p>
    <p v-if="!canApply && !canViewExisting" class="remote-auth-hint">
      当前账号没有远程网站评估权限，请联系管理员开通。
    </p>
    <div v-else class="remote-auth-actions">
      <button
        v-if="issue.authorizable && canApply"
        class="remote-auth-btn is-primary"
        type="button"
        @click="goApply(issue.origin)"
      >去申请授权</button>
      <button
        v-if="canViewExisting"
        class="remote-auth-btn"
        type="button"
        @click="goExisting"
      >查看已有授权</button>
    </div>
  </section>
</template>

<style scoped>
.remote-auth-notice {
  display: grid;
  gap: 4px;
  margin: 0 0 var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--sev-medium, #d9a857);
  border-radius: var(--r-md);
  background: var(--sev-medium-bg, #fff8e6);
}
.remote-auth-title { font-size: var(--fs-sm); }
.remote-auth-text,
.remote-auth-tasks,
.remote-auth-hint { margin: 0; line-height: 1.55; overflow-wrap: anywhere; }
.remote-auth-origin {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}
.remote-auth-tasks,
.remote-auth-hint { color: var(--color-text-secondary); }
.remote-auth-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-top: 4px; }
.remote-auth-btn {
  padding: 3px 10px;
  border: 1px solid var(--color-border-base);
  border-radius: var(--r-sm);
  background: var(--color-bg-card);
  color: var(--color-text-primary);
  font: inherit;
  cursor: pointer;
}
.remote-auth-btn:hover { border-color: var(--brand-300); color: var(--brand-600); background: var(--brand-50); }
.remote-auth-btn.is-primary { border-color: var(--brand-300); color: var(--brand-600); background: var(--brand-50); }
</style>
