<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, Close, EditPen, Refresh, RefreshLeft, SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import EmptyState from '@/components/common/EmptyState.vue'
import {
  approveAgentRelease,
  disableCustomAgent,
  listAdminAgentReleases,
  listAgentReleaseApprovals,
  rejectAgentRelease,
  reviseAgentRelease,
  rollbackCustomAgent,
} from '@/api/agentStudio'
import type {
  AdminAgentReleases,
  AgentReleaseApproval,
  AgentReleaseAuthoring,
} from '@/types/agentStudio'

const activeTab = ref<'approvals' | 'releases'>('approvals')
const loading = ref(false)
const approvals = ref<AgentReleaseApproval[]>([])
const agents = ref<AdminAgentReleases[]>([])
const selected = ref<AgentReleaseApproval | null>(null)
const drawerVisible = ref(false)
const reviseVisible = ref(false)
const actionKey = ref('')
const reviseForm = reactive({ prompt: '', review_focus: '', temperature: 0.2, max_tokens: 4096, note: '' })

const pendingCount = computed(() => approvals.value.filter((item) => item.status === 'pending').length)
const actionBusy = computed(() => actionKey.value.length > 0)
const firstRelease = computed(() => !selected.value?.diff.from_version)
const beforeAuthoring = computed<AgentReleaseAuthoring | null>(() => {
  const row = selected.value
  return row?.previous_authoring ?? row?.before_authoring ?? row?.diff.before ?? null
})
const afterAuthoring = computed<AgentReleaseAuthoring | null>(() => (
  selected.value?.authoring ?? selected.value?.diff.after ?? null
))

// ── 中文标签映射(卡片化后替代原始枚举展示,颜色语义沿用原 tag) ──
const approvalStatusLabels: Record<string, string> = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已驳回',
}
const approvalStatusTypes: Record<string, 'warning' | 'success' | 'info'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'info',
}
const riskLevelLabels: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
}
const assetStatusLabels: Record<string, string> = {
  draft: '草稿',
  testing: '测试中',
  pending_approval: '待审批',
  published: '已发布',
  disabled: '已停用',
  rolled_back: '已回滚',
  rejected: '已驳回',
}

function approvalStatusLabel(status: string): string {
  return approvalStatusLabels[status] ?? status
}

function approvalStatusType(status: string): 'warning' | 'success' | 'info' {
  return approvalStatusTypes[status] ?? 'info'
}

function riskLabel(level: string): string {
  return riskLevelLabels[level] ?? (level || '未评级')
}

function assetStatusLabel(status: string): string {
  return assetStatusLabels[status] ?? status
}

// ── 提交说明展开/收起(长文本不截断丢失,默认单行省略+title 悬停) ──
const expandedIds = ref<number[]>([])

function isExpanded(id: number): boolean {
  return expandedIds.value.includes(id)
}

function toggleExpand(id: number): void {
  const index = expandedIds.value.indexOf(id)
  if (index >= 0) expandedIds.value.splice(index, 1)
  else expandedIds.value.push(id)
}

function isCancelled(error: unknown): boolean {
  return error === 'cancel' || error === 'close'
    || (error instanceof Error && ['cancel', 'close'].includes(error.message))
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message
  if (error && typeof error === 'object') {
    const message = (error as Record<string, unknown>).message
    if (typeof message === 'string' && message) return message
  }
  return fallback
}

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[approvals.value, agents.value] = await Promise.all([
      listAgentReleaseApprovals(),
      listAdminAgentReleases(),
    ])
  } catch (error) {
    ElMessage.error(errorMessage(error, '发布审批数据加载失败'))
  } finally {
    loading.value = false
  }
}

function openDetail(row: AgentReleaseApproval): void {
  selected.value = row
  drawerVisible.value = true
}

async function decide(row: AgentReleaseApproval, approve: boolean): Promise<void> {
  if (actionBusy.value) return
  const action = approve ? '批准并发布' : '驳回'
  actionKey.value = `decision-${row.id}`
  try {
    const { value } = await ElMessageBox.prompt(`填写${action}意见`, action, {
      inputPlaceholder: '审批依据',
      confirmButtonText: action,
      cancelButtonText: '取消',
      inputType: 'textarea',
    })
    if (approve) await approveAgentRelease(row.id, value)
    else await rejectAgentRelease(row.id, value)
    ElMessage.success(approve ? '发布完成' : '已驳回')
    drawerVisible.value = false
    await load()
  } catch (error) {
    if (!isCancelled(error)) ElMessage.error(errorMessage(error, `${action}失败`))
  } finally {
    actionKey.value = ''
  }
}

function openRevise(row: AgentReleaseApproval): void {
  selected.value = row
  Object.assign(reviseForm, {
    prompt: row.authoring?.prompt || '',
    review_focus: row.authoring?.review_focus || '',
    temperature: Number(row.authoring?.model_config.temperature ?? 0.2),
    max_tokens: Number(row.authoring?.model_config.max_tokens ?? 4096),
    note: '',
  })
  reviseVisible.value = true
}

async function submitRevision(): Promise<void> {
  if (!selected.value || actionBusy.value) return
  actionKey.value = `revise-${selected.value.id}`
  try {
    await reviseAgentRelease(selected.value.id, {
      prompt: reviseForm.prompt,
      review_focus: reviseForm.review_focus,
      model_config_json: { temperature: reviseForm.temperature, max_tokens: reviseForm.max_tokens },
      note: reviseForm.note,
    })
    ElMessage.success('管理员修订版已创建，必须重新测试后提交')
    reviseVisible.value = false
    drawerVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error, '创建管理员修订版失败'))
  } finally {
    actionKey.value = ''
  }
}

async function disableAgent(item: AdminAgentReleases): Promise<void> {
  if (actionBusy.value) return
  actionKey.value = `disable-${item.agent.id}`
  try {
    await ElMessageBox.confirm(`确认停用 ${item.agent.name}？新任务将不再调用。`, '停用 Agent', { type: 'warning' })
    await disableCustomAgent(item.agent.id)
    ElMessage.success('Agent 已停用')
    await load()
  } catch (error) {
    if (!isCancelled(error)) ElMessage.error(errorMessage(error, '停用 Agent 失败'))
  } finally {
    actionKey.value = ''
  }
}

async function rollback(item: AdminAgentReleases, releaseId: number): Promise<void> {
  if (actionBusy.value) return
  actionKey.value = `rollback-${releaseId}`
  try {
    await ElMessageBox.confirm(`确认回滚到发布 #${releaseId}？`, '版本回滚', { type: 'warning' })
    await rollbackCustomAgent(item.agent.id, releaseId)
    ElMessage.success('已创建回滚发布')
    await load()
  } catch (error) {
    if (!isCancelled(error)) ElMessage.error(errorMessage(error, '创建回滚发布失败'))
  } finally {
    actionKey.value = ''
  }
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function changeLabel(changed: boolean): string {
  if (firstRelease.value) return '新增'
  return changed ? '有变更' : '无变更'
}

function changeTagType(changed: boolean): 'success' | 'warning' | 'info' {
  if (firstRelease.value) return 'success'
  return changed ? 'warning' : 'info'
}

function beforeText(field: keyof AgentReleaseAuthoring): string {
  if (firstRelease.value) return '首次发布，无前一版本'
  const value = beforeAuthoring.value?.[field]
  if (value === undefined || value === null) return '接口未返回前一版本内容'
  return field === 'model_config' ? json(value) : String(value) || '（空）'
}

function afterText(field: keyof AgentReleaseAuthoring): string {
  const value = afterAuthoring.value?.[field]
  if (value === undefined || value === null) return '接口未返回当前版本内容'
  return field === 'model_config' ? json(value) : String(value) || '（空）'
}

onMounted(load)
</script>

<template>
  <div class="release-page">
    <header class="page-header">
      <div>
        <h2>Agent 发布审批</h2>
        <p>待处理 {{ pendingCount }} 项</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-segmented v-model="activeTab" :options="[{ label: '发布审批', value: 'approvals' }, { label: '发布与回滚', value: 'releases' }]" />

    <section v-if="activeTab === 'approvals'" class="data-section">
      <div v-loading="loading" class="approval-cards" role="list" data-testid="approval-cards">
        <EmptyState v-if="!approvals.length" description="暂无发布审批" />
        <article
          v-for="row in approvals"
          :key="row.id"
          class="approval-card"
          :data-status="row.status"
          role="listitem"
          @click="openDetail(row)"
        >
          <span class="rc-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="rc-main">
            <div class="rc-line1">
              <span class="rc-id font-mono">#{{ row.id }}</span>
              <b class="rc-name" :title="row.agent?.name || ''">{{ row.agent?.name || '-' }}</b>
              <el-tag size="small" type="info" effect="plain">v{{ row.version?.version_number || '-' }}</el-tag>
              <el-tag size="small" type="warning" effect="plain">{{ riskLabel(row.risk.level) }}</el-tag>
              <el-tag size="small" :type="approvalStatusType(row.status)" effect="plain">{{ approvalStatusLabel(row.status) }}</el-tag>
            </div>
            <div class="rc-line2 font-mono">
              <span class="rc-code">{{ row.agent?.code || '—' }}</span>
            </div>
            <div class="rc-note" @click.stop>
              <span class="rc-note-label">提交说明</span>
              <span class="rc-note-text" :class="{ 'is-expanded': isExpanded(row.id) }" :title="row.title">{{ row.title || '（未填写提交说明）' }}</span>
              <el-button v-if="(row.title || '').length > 42" link size="small" class="rc-note-toggle" @click="toggleExpand(row.id)">{{ isExpanded(row.id) ? '收起' : '展开' }}</el-button>
            </div>
          </div>
          <div class="rc-metrics" :title="`精确依赖 ${row.dependencies.length} 项，新增调用约 ${row.estimated_calls_per_chunk} 次/代码分片，能力申请见审批抽屉`">
            <div class="rc-metric" data-testid="approval-metric-skills">
              <b>{{ row.dependencies.length }}</b>
              <small>Skill 依赖</small>
            </div>
            <div class="rc-metric" data-testid="approval-metric-calls">
              <b>+{{ row.estimated_calls_per_chunk }}</b>
              <small>新增调用/分片</small>
            </div>
          </div>
          <div class="rc-actions" @click.stop>
            <template v-if="row.status === 'pending'">
              <el-button
                size="small"
                type="danger"
                plain
                :loading="actionKey === `decision-${row.id}`"
                :disabled="actionBusy && actionKey !== `decision-${row.id}`"
                @click="decide(row, false)"
              >驳回</el-button>
              <el-button
                size="small"
                type="primary"
                :loading="actionKey === `decision-${row.id}`"
                :disabled="actionBusy && actionKey !== `decision-${row.id}`"
                @click="decide(row, true)"
              >批准</el-button>
            </template>
            <el-button text type="primary" size="small" @click="openDetail(row)">查看</el-button>
          </div>
        </article>
      </div>
    </section>

    <section v-else class="release-list" v-loading="loading">
      <article v-for="item in agents" :key="item.agent.id" class="release-row">
        <div class="release-agent">
          <b>{{ item.agent.name }}</b><code>{{ item.agent.code }}</code>
          <el-tag size="small" effect="plain">{{ assetStatusLabel(item.agent.status) }}</el-tag>
        </div>
        <div class="release-versions">
          <button v-for="release in item.releases" :key="release.id" type="button" class="release-chip" :disabled="actionBusy" @click="rollback(item, release.id)">
            <span>#{{ release.id }} · vID {{ release.agent_version_id }}</span>
            <small>{{ assetStatusLabel(release.status) }}</small>
          </button>
        </div>
        <el-button v-if="item.agent.is_enabled" type="danger" plain :icon="SwitchButton" :loading="actionKey === `disable-${item.agent.id}`" :disabled="actionBusy && actionKey !== `disable-${item.agent.id}`" @click="disableAgent(item)">停用</el-button>
      </article>
    </section>

    <el-drawer v-model="drawerVisible" title="发布包审查" size="min(760px, 96vw)" append-to-body :z-index="4200" :close-on-click-modal="false">
      <template v-if="selected">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="Agent">{{ selected.agent?.name }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ selected.version?.version_number }}</el-descriptions-item>
          <el-descriptions-item label="前一版本">{{ selected.diff.from_version ? `v${selected.diff.from_version}` : '首次发布' }}</el-descriptions-item>
          <el-descriptions-item label="预计调用">+{{ selected.estimated_calls_per_chunk }}/代码分片</el-descriptions-item>
          <el-descriptions-item label="能力申请" :span="2">{{ selected.risk.requested_capabilities.join(', ') || '无' }}</el-descriptions-item>
        </el-descriptions>
        <div class="review-block release-diff">
          <div class="diff-heading"><h3>系统提示词</h3><el-tag :type="changeTagType(selected.diff.prompt_changed)" effect="plain">{{ changeLabel(selected.diff.prompt_changed) }}</el-tag></div>
          <div class="diff-comparison">
            <section><b>变更前</b><pre class="diff-before">{{ beforeText('prompt') }}</pre></section>
            <section><b>变更后</b><pre class="diff-after">{{ afterText('prompt') }}</pre></section>
          </div>
        </div>
        <div class="review-block release-diff">
          <div class="diff-heading"><h3>审查重点</h3><el-tag :type="changeTagType(selected.diff.review_focus_changed)" effect="plain">{{ changeLabel(selected.diff.review_focus_changed) }}</el-tag></div>
          <div class="diff-comparison">
            <section><b>变更前</b><pre class="diff-before">{{ beforeText('review_focus') }}</pre></section>
            <section><b>变更后</b><pre class="diff-after">{{ afterText('review_focus') }}</pre></section>
          </div>
        </div>
        <div class="review-block release-diff">
          <div class="diff-heading"><h3>模型参数</h3><el-tag :type="changeTagType(selected.diff.model_config_changed)" effect="plain">{{ changeLabel(selected.diff.model_config_changed) }}</el-tag></div>
          <div class="diff-comparison">
            <section><b>变更前</b><pre class="diff-before">{{ beforeText('model_config') }}</pre></section>
            <section><b>变更后</b><pre class="diff-after">{{ afterText('model_config') }}</pre></section>
          </div>
        </div>
        <div class="review-block"><h3>{{ selected.test_evidence_kind === 'static_contract' ? '静态契约检查证据' : '测试证据' }}</h3><pre>{{ json(selected.test_evidence) }}</pre></div>
        <div class="review-block"><h3>精确依赖</h3><pre>{{ json(selected.dependencies) }}</pre></div>
        <div v-if="selected.status === 'pending'" class="drawer-actions">
          <el-button :icon="EditPen" :disabled="actionBusy" @click="openRevise(selected)">管理员修订</el-button>
          <el-button type="danger" :icon="Close" :loading="actionKey === `decision-${selected.id}`" :disabled="actionBusy" @click="decide(selected, false)">驳回</el-button>
          <el-button type="primary" :icon="Check" :loading="actionKey === `decision-${selected.id}`" :disabled="actionBusy" @click="decide(selected, true)">批准并发布</el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="reviseVisible" title="创建管理员修订版" width="min(760px, 94vw)" append-to-body :z-index="4300" :close-on-click-modal="false">
      <el-form :model="reviseForm" label-position="top">
        <el-form-item label="系统提示词"><el-input v-model="reviseForm.prompt" type="textarea" :rows="10" /></el-form-item>
        <el-form-item label="审查重点"><el-input v-model="reviseForm.review_focus" type="textarea" :rows="5" /></el-form-item>
        <div class="model-grid">
          <el-form-item label="Temperature"><el-input-number v-model="reviseForm.temperature" :min="0" :max="1" :step="0.1" /></el-form-item>
          <el-form-item label="最大 Token"><el-input-number v-model="reviseForm.max_tokens" :min="128" :max="4096" :step="128" /></el-form-item>
        </div>
        <el-form-item label="修订说明"><el-input v-model="reviseForm.note" maxlength="500" /></el-form-item>
      </el-form>
      <template #footer><el-button :disabled="actionBusy" @click="reviseVisible = false">取消</el-button><el-button type="primary" :icon="RefreshLeft" :loading="actionKey.startsWith('revise-')" :disabled="actionBusy && !actionKey.startsWith('revise-')" @click="submitRevision">创建修订版</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.release-page { display: grid; gap: 18px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.page-header h2 { margin: 0; font-size: 24px; }
.page-header p { margin: 6px 0 0; color: var(--gray-500); }
.data-section { border-top: 1px solid var(--gray-200); background: #fff; }

/* ── 审批卡片列表(替代表格:Agent+版本+风险为主行,指标右置,提交说明次行可展开) ── */
.approval-cards { display: grid; gap: 10px; min-height: 96px; }
.approval-card {
  position: relative; display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: 16px; align-items: center;
  padding: 13px 16px 13px 12px; border-radius: 12px;
  background: #fff; border: 1px solid var(--gray-100, #eef0f4);
  cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.approval-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.rc-band { width: 4px; height: 40px; border-radius: 999px; }
.rc-band[data-status='pending'] { background: #d9a857; }
.rc-band[data-status='approved'] { background: #40a35f; }
.rc-band[data-status='rejected'] { background: var(--gray-300, #cfd4dc); }
.rc-main { display: grid; gap: 5px; min-width: 0; }
.rc-line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rc-id { font-size: 12px; color: var(--gray-500); }
.rc-name { font-size: 13.5px; font-weight: 600; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-line2 { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--gray-500); }
.rc-code { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-note { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.rc-note-label { flex: none; font-size: 11px; color: var(--gray-400); }
.rc-note-text { min-width: 0; max-width: 560px; font-size: 12px; color: var(--gray-500); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-note-text.is-expanded { white-space: normal; overflow: visible; }
.rc-metrics { display: flex; gap: 20px; }
.rc-metric { display: grid; justify-items: center; gap: 2px; min-width: 62px; text-align: center; }
.rc-metric b { font-size: 15px; font-weight: 700; }
.rc-metric small { font-size: 11px; color: var(--gray-500); }
.rc-actions { display: flex; gap: 4px; align-items: center; }

@media (prefers-reduced-motion: reduce) {
  .approval-card { transition: none; }
}
.release-list { display: grid; border-top: 1px solid var(--gray-200); }
.release-row { min-height: 86px; display: grid; grid-template-columns: 210px 1fr auto; align-items: center; gap: 18px; padding: 14px 0; border-bottom: 1px solid var(--gray-200); }
.release-agent { display: grid; gap: 3px; justify-items: start; }
.release-agent code { color: var(--gray-500); font-size: 11px; }
.release-versions { display: flex; gap: 8px; overflow-x: auto; }
.release-chip { min-width: 150px; display: grid; gap: 3px; padding: 8px 10px; border: 1px solid var(--gray-200); border-radius: 6px; background: #fff; text-align: left; cursor: pointer; }
.release-chip:hover { border-color: var(--brand-400); }
.release-chip small { color: var(--gray-500); }
.review-block { margin-top: 22px; }
.review-block h3 { font-size: 14px; margin: 0 0 10px; }
.review-block pre { max-height: 220px; overflow: auto; margin: 0; padding: 12px; background: var(--gray-50); border: 1px solid var(--gray-200); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.diff-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.diff-heading h3 { margin: 0; }
.diff-comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; margin-top: 10px; }
.diff-comparison section { min-width: 0; }
.diff-comparison b { display: block; margin-bottom: 5px; color: var(--gray-600); font-size: 11px; }
.diff-comparison pre { min-height: 92px; max-height: 280px; }
.diff-comparison .diff-before { background: #fff8f7; border-color: #f0d5d1; }
.diff-comparison .diff-after { background: #f2f9f5; border-color: #cfe6d7; }
.release-chip:disabled { opacity: .5; cursor: not-allowed; }
.drawer-actions { position: sticky; bottom: 0; display: flex; justify-content: flex-end; gap: 8px; margin-top: 24px; padding: 16px 0; background: #fff; border-top: 1px solid var(--gray-200); }
.model-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 760px) {
  .page-header { flex-direction: column; }
  .approval-card { grid-template-columns: auto minmax(0, 1fr); gap: 10px 12px; }
  .rc-metrics { grid-column: 2; justify-content: flex-start; }
  .rc-actions { grid-column: 1 / -1; justify-content: flex-end; flex-wrap: wrap; }
  .rc-name { max-width: 46vw; }
  .rc-note-text { max-width: 60vw; }
  .release-row { grid-template-columns: 1fr; }
  .model-grid { grid-template-columns: 1fr; }
  .diff-comparison { grid-template-columns: 1fr; }
  .drawer-actions { flex-wrap: wrap; }
}
</style>
