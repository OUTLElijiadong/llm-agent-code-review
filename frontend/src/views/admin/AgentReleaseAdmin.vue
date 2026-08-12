<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, Close, EditPen, Refresh, RefreshLeft, SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

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
import { agentApprovalStatusLabel, agentAssetStatusLabel } from '@/constants/agentStatus'

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

/** 模型参数常用键的中文标签,JSON 键值对展示时先翻译 */
const MODEL_CONFIG_LABELS: Record<string, string> = {
  temperature: '采样温度',
  max_tokens: '最大输出 Token',
  top_p: '核采样 Top-P',
  model: '模型',
  provider: '提供商',
}

/**
 * 将 model_config 对象转换为键值对列表,键尽量翻译为中文
 * @param value - model_config 对象
 * @returns 键值对数组
 */
function modelConfigEntries(value: unknown): Array<{ key: string; label: string; text: string }> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value as Record<string, unknown>).map(([key, val]) => ({
    key,
    label: MODEL_CONFIG_LABELS[key] ?? key,
    text: val === null || val === undefined ? '（空）' : typeof val === 'object' ? JSON.stringify(val) : String(val),
  }))
}

const beforeModelConfig = computed(() => modelConfigEntries(beforeAuthoring.value?.model_config))
const afterModelConfig = computed(() => modelConfigEntries(afterAuthoring.value?.model_config))

/**
 * 将测试证据对象格式化为键值对列表,避免直接裸展示 JSON
 * @param value - test_evidence 对象
 * @returns 键值对数组(嵌套对象压缩为一行 JSON)
 */
function evidenceEntries(value: unknown): Array<{ key: string; text: string }> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value as Record<string, unknown>).map(([key, val]) => ({
    key,
    text: typeof val === 'object' && val !== null ? JSON.stringify(val) : String(val),
  }))
}

const testEvidenceEntries = computed(() => evidenceEntries(selected.value?.test_evidence))

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
      <el-table v-loading="loading" :data="approvals" stripe empty-text="暂无发布审批">
        <el-table-column prop="id" label="审批" width="90"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column label="Agent" min-width="190">
          <template #default="{ row }"><b>{{ row.agent?.name || '-' }}</b><div class="subtle"><code>{{ row.agent?.code }}</code></div></template>
        </el-table-column>
        <el-table-column label="版本" width="100"><template #default="{ row }">v{{ row.version?.version_number || '-' }}</template></el-table-column>
        <el-table-column label="Skill" width="90"><template #default="{ row }">{{ row.dependencies.length }}</template></el-table-column>
        <el-table-column label="新增调用" width="110"><template #default="{ row }">+{{ row.estimated_calls_per_chunk }}/分片</template></el-table-column>
        <el-table-column label="风险" width="100"><template #default="{ row }"><el-tag type="warning" effect="plain">{{ row.risk.level }}</el-tag></template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'pending' ? 'warning' : row.status === 'approved' ? 'success' : 'info'" effect="plain">{{ agentApprovalStatusLabel(row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="110" fixed="right"><template #default="{ row }"><el-button text type="primary" @click="openDetail(row)">查看</el-button></template></el-table-column>
      </el-table>
    </section>

    <section v-else class="release-list" v-loading="loading">
      <article v-for="item in agents" :key="item.agent.id" class="release-row">
        <div class="release-agent">
          <b>{{ item.agent.name }}</b><code>{{ item.agent.code }}</code>
          <el-tag size="small" effect="plain">{{ agentAssetStatusLabel(item.agent.status) }}</el-tag>
        </div>
        <div class="release-versions">
          <button v-for="release in item.releases" :key="release.id" type="button" class="release-chip" :disabled="actionBusy" @click="rollback(item, release.id)">
            <span>#{{ release.id }} · vID {{ release.agent_version_id }}</span>
            <small>{{ agentApprovalStatusLabel(release.status) }}</small>
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
.subtle { color: var(--gray-500); font-size: 12px; margin-top: 3px; }
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
  .release-row { grid-template-columns: 1fr; }
  .model-grid { grid-template-columns: 1fr; }
  .diff-comparison { grid-template-columns: 1fr; }
  .drawer-actions { flex-wrap: wrap; }
}
</style>
