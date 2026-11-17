<template>
  <div class="copilot-security-page">
    <el-alert
      v-if="policy?.kill_switch"
      type="error"
      :closable="false"
      show-icon
      :title="`管理小菱已紧急停机：${policy.kill_switch_reason || '未填写原因'}`"
      class="kill-banner"
    />

    <div class="page-header">
      <div>
        <h2>小菱安全中心</h2>
        <p class="page-sub">管理小菱（admin_copilot）的安全口径：看态势、切预设、调策略、做演练。</p>
      </div>
    </div>

    <!-- 1. 态势头 -->
    <el-card v-loading="loading" shadow="hover" class="posture-card">
      <div class="posture-head">
        <div class="posture-score">
          <span class="score-number" data-testid="posture-score">{{ posture?.score ?? '--' }}</span>
          <el-tag
            v-if="posture"
            size="large"
            effect="dark"
            :type="gradeTagType(posture.grade)"
            data-testid="posture-grade"
          >
            {{ posture.grade }}
          </el-tag>
          <el-progress
            :percentage="posture?.score ?? 0"
            :color="gradeColor(posture?.grade)"
            :stroke-width="10"
            class="score-bar"
          />
        </div>
        <div class="posture-actions">
          <el-tooltip :disabled="canWrite" content="仅超级管理员可修改" placement="top">
            <span>
              <el-button
                data-testid="kill-switch-button"
                :type="policy?.kill_switch ? 'success' : 'danger'"
                :disabled="!canWrite || saving"
                @click="onToggleKillSwitch"
              >
                {{ policy?.kill_switch ? '解除停机' : '紧急停机' }}
              </el-button>
            </span>
          </el-tooltip>
          <el-button data-testid="reload-button" :loading="loading" @click="loadCenter">重新读取</el-button>
        </div>
      </div>
      <p v-if="posture" class="posture-meta">
        近 {{ posture.events.window_hours }} 小时事件 {{ posture.events.total }} 条，
        其中拦截 {{ posture.events.blocked }} 条 · 生成于 {{ posture.generated_at }}
      </p>
    </el-card>

    <!-- 2. 预设区 -->
    <el-card shadow="hover" class="section-card">
      <template #header>预设</template>
      <div class="preset-grid" data-testid="preset-grid">
        <el-tooltip
          v-for="item in center?.presets ?? []"
          :key="item.preset"
          :disabled="canWrite"
          content="仅超级管理员可修改"
          placement="top"
        >
          <button
            type="button"
            class="preset-card"
            :class="{ 'is-current': policy?.preset === item.preset }"
            :data-preset="item.preset"
            :disabled="!canWrite || saving"
            @click="onApplyPreset(item.preset)"
          >
            <b>{{ item.label }}</b>
            <span class="preset-code">{{ item.preset }}</span>
            <el-tag v-if="policy?.preset === item.preset" size="small" type="success">当前</el-tag>
          </button>
        </el-tooltip>
      </div>
    </el-card>

    <!-- 3. 策略表单 -->
    <el-card shadow="hover" class="section-card">
      <template #header>
        <div class="card-head-row">
          <span>策略</span>
          <div v-if="form" class="policy-head-controls">
            <el-tag size="small" type="info" data-testid="policy-mode-tag">
              模式 {{ form?.mode ?? '--' }}
            </el-tag>
            <el-tooltip :disabled="canWrite" content="仅超级管理员可修改" placement="top">
              <span class="policy-enabled-switch">
                <span class="switch-label">安全策略启用</span>
                <el-switch
                  v-model="form.enabled"
                  data-testid="policy-enabled-switch"
                  :disabled="!canWrite"
                />
              </span>
            </el-tooltip>
          </div>
          <el-tooltip :disabled="canWrite" content="仅超级管理员可修改" placement="top">
            <span>
              <el-button
                type="primary"
                data-testid="save-policy-button"
                :disabled="!canWrite || saving"
                :loading="saving"
                @click="onSavePolicy"
              >
                保存改动
              </el-button>
            </span>
          </el-tooltip>
        </div>
      </template>
      <el-collapse v-if="form" v-model="openGroups">
        <el-collapse-item title="会话防护" name="session">
          <el-form label-width="200px" :disabled="!canWrite">
            <el-form-item label="生效入口">
              <el-checkbox-group v-model="form.session_guard_surfaces">
                <el-checkbox value="user" label="用户端" />
                <el-checkbox value="admin" label="管理端" />
              </el-checkbox-group>
            </el-form-item>
            <el-form-item label="IP 白名单">
              <el-input
                v-model="ipAllowlistText"
                type="textarea"
                :rows="3"
                placeholder="一行一个 IP 或 CIDR，合法性由后端校验"
              />
            </el-form-item>
            <el-form-item label="会话绑定 IP">
              <el-switch v-model="form.bind_session_to_ip" />
            </el-form-item>
            <el-form-item label="空闲超时（秒）">
              <el-input-number v-model="form.session_idle_timeout_seconds" :min="0" :step="60" />
            </el-form-item>
            <el-form-item label="绝对超时（秒）">
              <el-input-number v-model="form.session_absolute_timeout_seconds" :min="0" :step="3600" />
            </el-form-item>
            <el-form-item label="消息字符上限">
              <el-input-number v-model="form.max_message_chars" :min="0" :step="1000" />
            </el-form-item>
            <el-form-item label="信任代理头">
              <el-switch v-model="form.trust_proxy_headers" />
            </el-form-item>
          </el-form>
        </el-collapse-item>

        <el-collapse-item title="提示注入" name="injection">
          <el-form label-width="200px" :disabled="!canWrite">
            <el-form-item label="启用注入检测">
              <el-switch v-model="form.injection_scan_enabled" />
            </el-form-item>
            <el-form-item label="拦截阈值">
              <el-slider v-model="form.injection_block_threshold" :min="0" :max="100" show-input />
            </el-form-item>
            <el-form-item label="告警阈值">
              <el-slider v-model="form.injection_warn_threshold" :min="0" :max="100" show-input />
            </el-form-item>
            <el-form-item label="拦截生效入口">
              <el-checkbox-group v-model="form.injection_blocked_surfaces">
                <el-checkbox value="user" label="用户端" />
                <el-checkbox value="admin" label="管理端" />
              </el-checkbox-group>
            </el-form-item>
          </el-form>
        </el-collapse-item>

        <el-collapse-item title="工具闸门" name="tools">
          <el-form label-width="200px" :disabled="!canWrite">
            <el-form-item label="工具白名单">
              <el-input
                v-model="toolAllowlistText"
                type="textarea"
                :rows="2"
                placeholder="工具编码，逗号或换行分隔；留空表示不限制"
              />
            </el-form-item>
            <el-form-item label="工具黑名单">
              <el-input
                v-model="toolDenylistText"
                type="textarea"
                :rows="2"
                placeholder="工具编码，逗号或换行分隔"
              />
            </el-form-item>
            <el-form-item label="禁止 shell">
              <el-switch v-model="form.deny_shell" />
            </el-form-item>
            <el-form-item label="禁止改生产配置">
              <el-switch v-model="form.deny_production_config" />
            </el-form-item>
            <el-form-item label="禁止写操作">
              <el-switch v-model="form.deny_write_actions" />
            </el-form-item>
            <el-form-item label="写操作转审批">
              <el-switch v-model="form.require_approval_for_writes" />
            </el-form-item>
            <el-form-item label="危险工具需审批">
              <el-switch v-model="form.dangerous_tools_require_approval" />
            </el-form-item>
            <el-form-item label="单轮调用上限">
              <el-input-number v-model="form.max_tool_calls_per_turn" :min="0" />
            </el-form-item>
            <el-form-item label="每小时调用上限">
              <el-input-number v-model="form.max_tool_calls_per_hour" :min="0" />
            </el-form-item>
          </el-form>
        </el-collapse-item>

        <el-collapse-item title="审计与脱敏" name="audit">
          <el-form label-width="200px" :disabled="!canWrite">
            <el-form-item label="记录安全事件">
              <el-switch v-model="form.audit_events_enabled" />
            </el-form-item>
            <el-form-item label="日志脱敏">
              <el-switch v-model="form.redaction_enabled" />
            </el-form-item>
            <el-form-item label="审计详情脱敏">
              <el-switch v-model="form.redact_in_audit" />
            </el-form-item>
            <el-form-item label="拦截时告警">
              <el-switch v-model="form.alert_on_block" />
            </el-form-item>
            <el-form-item label="二次确认窗口（秒）">
              <el-input-number v-model="form.step_up_window_seconds" :min="0" :step="60" />
            </el-form-item>
            <el-form-item label="受治理的 Agent">
              <el-input
                v-model="governedAgentsText"
                type="textarea"
                :rows="2"
                placeholder="Agent 编码，逗号或换行分隔"
              />
            </el-form-item>
          </el-form>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 4. 整改清单 -->
    <el-card v-if="posture" shadow="hover" class="section-card" data-testid="remediation-card">
      <template #header>整改清单</template>
      <el-empty v-if="!posture.checks.length" description="暂无检查项" />
      <div v-for="check in gapChecks" :key="check.key" class="gap-row">
        <el-alert type="error" :closable="false" show-icon>
          <template #title>{{ check.title }}</template>
          {{ check.remediation }}
        </el-alert>
      </div>
      <el-collapse v-if="passedChecks.length">
        <el-collapse-item :title="`已满足（${passedChecks.length}）`" name="passed">
          <ul class="passed-list">
            <li v-for="check in passedChecks" :key="check.key">{{ check.title }}</li>
          </ul>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 5. 注入演练 -->
    <el-card shadow="hover" class="section-card">
      <template #header>注入演练</template>
      <p class="drill-note">这是演练：只返回命中与脱敏结果，不会落库，也不会拦截任何真实请求。</p>
      <el-input
        v-model="scanText"
        type="textarea"
        :rows="4"
        placeholder="粘贴一段要检测的输入"
        data-testid="scan-input"
      />
      <div class="drill-actions">
        <el-button type="primary" data-testid="scan-button" :loading="scanning" @click="onScan">演练</el-button>
      </div>
      <div v-if="scanResult" class="scan-result" data-testid="scan-result">
        <p>
          注入分值 <b>{{ scanResult.injection.score }}</b>
          <el-tag v-if="scanResult.injection.obfuscation" size="small" type="warning">疑似混淆</el-tag>
          · 原文 {{ scanResult.original_length }} 字符
        </p>
        <el-table :data="scanResult.injection.hits" size="small" empty-text="未命中任何规则">
          <el-table-column prop="code" label="规则" width="160" />
          <el-table-column prop="category" label="类别" width="120" />
          <el-table-column prop="score" label="分值" width="80" />
          <el-table-column prop="description" label="说明" />
          <el-table-column prop="evidence" label="证据" />
        </el-table>
        <p class="drill-note">脱敏后文本</p>
        <pre class="redacted-text">{{ scanResult.redacted }}</pre>
      </div>
    </el-card>

    <!-- 6. 事件列表 -->
    <el-card shadow="hover" class="section-card">
      <template #header>
        <div class="card-head-row">
          <span>安全事件</span>
          <!-- TODO(codex): 导出走 GET /admin/copilot-security/events/export?format=csv|json，
               由 copilot_security_analytics 提供，本期前端不发请求。 -->
          <el-button data-testid="export-button" disabled>导出</el-button>
        </div>
      </template>
      <div class="filter-bar">
        <el-select v-model="eventFilters.kind" placeholder="事件类型" clearable style="width: 200px" @change="reloadEvents">
          <el-option
            v-for="item in center?.event_kinds ?? []"
            :key="item.kind"
            :label="item.label"
            :value="item.kind"
          />
        </el-select>
        <el-select v-model="eventFilters.status" placeholder="状态" clearable style="width: 140px" @change="reloadEvents">
          <el-option label="拦截" value="failed" />
          <el-option label="记录" value="info" />
        </el-select>
        <el-select v-model="eventFilters.days" placeholder="时间范围" style="width: 140px" @change="reloadEvents">
          <el-option label="近 1 天" :value="1" />
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
        </el-select>
      </div>
      <el-table v-loading="eventsLoading" :data="events" size="small" empty-text="暂无安全事件">
        <el-table-column prop="create_time" label="时间" width="180" />
        <el-table-column label="类型" width="180">
          <template #default="{ row }">{{ eventKindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'failed' ? 'danger' : 'info'">
              {{ row.status === 'failed' ? '拦截' : '记录' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="actor_name" label="操作者" width="140" />
        <el-table-column prop="ip" label="IP" width="140" />
        <el-table-column prop="target" label="目标" />
        <el-table-column label="详情" width="90">
          <template #default="{ row }">
            <el-popover placement="left" trigger="click" :width="420">
              <template #reference>
                <el-button link type="primary" size="small">查看</el-button>
              </template>
              <pre class="event-detail">{{ formatDetail(row.detail) }}</pre>
            </el-popover>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="eventPage"
          v-model:page-size="eventPageSize"
          :total="eventTotal"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadEvents"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { actionableError } from '@/composables/withFeedback'
import {
  applyCopilotSecurityPreset,
  getCopilotSecurityCenter,
  listCopilotSecurityEvents,
  scanCopilotSecurityText,
  toggleCopilotSecurityKillSwitch,
  updateCopilotSecurityPolicy,
} from '@/api/copilotSecurity'
import {
  COPILOT_SECURITY_POLICY_KEYS,
  type CopilotSecurityCenterOut,
  type CopilotSecurityCheckOut,
  type CopilotSecurityEventOut,
  type CopilotSecurityPolicyIn,
  type CopilotSecurityPolicyOut,
  type CopilotSecurityPolicySavedOut,
  type CopilotSecurityPostureOut,
  type CopilotSecurityPreset,
  type CopilotSecurityScanOut,
  type PostureGrade,
} from '@/types/copilotSecurity'

const userStore = useUserStore()
const canWrite = computed(() => userStore.isSuperAdmin())

const loading = ref(false)
const saving = ref(false)
const center = ref<CopilotSecurityCenterOut | null>(null)
const policy = ref<CopilotSecurityPolicyOut | null>(null)
const posture = ref<CopilotSecurityPostureOut | null>(null)
const form = ref<CopilotSecurityPolicyOut | null>(null)
const openGroups = ref<string[]>(['session', 'injection', 'tools', 'audit'])

/** 列表类字段在表单里用文本编辑，提交时再拆回数组。 */
const ipAllowlistText = ref('')
const toolAllowlistText = ref('')
const toolDenylistText = ref('')
const governedAgentsText = ref('')

const events = ref<CopilotSecurityEventOut[]>([])
const eventsLoading = ref(false)
const eventPage = ref(1)
const eventPageSize = ref(20)
const eventTotal = ref(0)
const eventFilters = reactive<{ kind: string; status: string; days: number }>({
  kind: '',
  status: '',
  days: 7,
})

const scanText = ref('')
const scanning = ref(false)
const scanResult = ref<CopilotSecurityScanOut | null>(null)

const gapChecks = computed<CopilotSecurityCheckOut[]>(() => {
  const gaps = new Set(posture.value?.critical_gaps ?? [])
  return (posture.value?.checks ?? []).filter((check) => gaps.has(check.key))
})
const passedChecks = computed<CopilotSecurityCheckOut[]>(() =>
  (posture.value?.checks ?? []).filter((check) => check.passed),
)

function gradeTagType(grade: PostureGrade): 'success' | 'warning' | 'danger' | 'info' {
  if (grade === 'A+' || grade === 'A') return 'success'
  if (grade === 'B') return 'warning'
  if (grade === 'C') return 'warning'
  return 'danger'
}

function gradeColor(grade: PostureGrade | undefined): string {
  if (grade === 'A+' || grade === 'A') return '#67c23a'
  if (grade === 'B') return '#e6a23c'
  if (grade === 'C') return '#f0c040'
  return '#f56c6c'
}

function splitList(text: string): string[] {
  return text.split(/[\n,]/).map((item) => item.trim()).filter((item) => item.length > 0)
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index])
}

function applySnapshot(data: CopilotSecurityCenterOut): void {
  center.value = data
  policy.value = data.policy
  posture.value = data.posture
  form.value = { ...data.policy }
  ipAllowlistText.value = data.policy.ip_allowlist.join('\n')
  toolAllowlistText.value = data.policy.tool_allowlist.join('\n')
  toolDenylistText.value = data.policy.tool_denylist.join('\n')
  governedAgentsText.value = data.policy.governed_agents.join('\n')
}

function applySaved(saved: CopilotSecurityPolicySavedOut): void {
  if (!center.value) return
  applySnapshot({ ...center.value, policy: saved.policy, posture: saved.posture })
}

/** 只收集相对已生效策略发生变化的字段（PATCH 语义）。 */
function buildPatch(): CopilotSecurityPolicyIn {
  const current = policy.value
  const draft = form.value
  if (!current || !draft) return {}
  const edited: CopilotSecurityPolicyOut = {
    ...draft,
    ip_allowlist: splitList(ipAllowlistText.value),
    tool_allowlist: splitList(toolAllowlistText.value),
    tool_denylist: splitList(toolDenylistText.value),
    governed_agents: splitList(governedAgentsText.value),
  }
  const patch: CopilotSecurityPolicyIn = {}
  for (const key of COPILOT_SECURITY_POLICY_KEYS) {
    const before = current[key]
    const after = edited[key]
    const unchanged = Array.isArray(before) && Array.isArray(after)
      ? sameList(before, after)
      : before === after
    if (!unchanged) {
      Object.assign(patch, { [key]: after })
    }
  }
  return patch
}

function reportError(error: unknown, fallback: string): void {
  ElMessage.error(actionableError(error, fallback).message)
}

async function loadCenter(): Promise<void> {
  loading.value = true
  try {
    applySnapshot(await getCopilotSecurityCenter())
  } catch (error) {
    reportError(error, '安全中心加载失败，请重试')
  } finally {
    loading.value = false
  }
}

async function loadEvents(): Promise<void> {
  eventsLoading.value = true
  try {
    const data = await listCopilotSecurityEvents({
      page: eventPage.value,
      page_size: eventPageSize.value,
      kind: eventFilters.kind,
      status: eventFilters.status,
      days: eventFilters.days,
    })
    events.value = data.items
    eventTotal.value = data.total
  } catch (error) {
    reportError(error, '安全事件加载失败，请重试')
  } finally {
    eventsLoading.value = false
  }
}

function reloadEvents(): void {
  eventPage.value = 1
  void loadEvents()
}

function eventKindLabel(kind: string): string {
  return center.value?.event_kinds.find((item) => item.kind === kind)?.label ?? kind
}

function formatDetail(detail: CopilotSecurityEventOut['detail']): string {
  if (detail === null || detail === undefined) return '-'
  return typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2)
}

const PRESET_IMPACT: Record<CopilotSecurityPreset, string> = {
  monitor: '只记录不拦截（阻断阈值 101），已有的拦截会全部放开。',
  balanced: '拦明显注入、禁 shell 与生产配置，不设调用预算。',
  strict: '注入阈值 50、绑定 IP、空闲 1 小时 / 绝对 12 小时、写操作转审批、单轮 40 次 / 每小时 240 次。',
  max: '最严口径：注入阈值 40、绑定 IP、空闲 30 分钟 / 绝对 8 小时、写操作转审批、单轮 24 次 / 每小时 120 次、消息 20000 字符。',
}

async function onApplyPreset(preset: CopilotSecurityPreset): Promise<void> {
  if (preset === policy.value?.preset) return
  try {
    await ElMessageBox.confirm(
      `套用「${preset}」预设：${PRESET_IMPACT[preset]}手填的 IP 白名单、工具名单与停机开关会保留。`,
      '切换预设',
      { type: 'warning', confirmButtonText: '套用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  let confirm = false
  if (preset === 'max') {
    try {
      const input = await ElMessageBox.prompt('切换到最顶级预设需要输入确认文本', '确认最顶级', {
        inputPlaceholder: '确认最顶级',
        confirmButtonText: '确认切换',
        cancelButtonText: '取消',
        inputValidator: (value: string) => value.trim() === '确认最顶级' || '请准确输入「确认最顶级」',
      })
      if (input.value.trim() !== '确认最顶级') return
      confirm = true
    } catch {
      return
    }
  }
  saving.value = true
  try {
    const saved = await applyCopilotSecurityPreset({ preset, confirm })
    applySaved(saved)
    ElMessage.success(saved.changed.length ? `已切换预设，改动 ${saved.changed.length} 项` : '已是该预设，无改动')
  } catch (error) {
    reportError(error, '切换预设失败，请重试')
  } finally {
    saving.value = false
  }
}

async function onToggleKillSwitch(): Promise<void> {
  const turningOn = !policy.value?.kill_switch
  let reason = ''
  try {
    if (turningOn) {
      const input = await ElMessageBox.prompt('停机后管理小菱的一切请求与工具调用会立即被拒', '紧急停机', {
        inputPlaceholder: '停机原因（必填）',
        confirmButtonText: '确认停机',
        cancelButtonText: '取消',
        inputValidator: (value: string) => value.trim().length > 0 || '请填写停机原因',
      })
      reason = input.value.trim()
    } else {
      await ElMessageBox.confirm('解除停机后管理小菱恢复按当前策略运行。', '解除停机', { type: 'warning' })
    }
  } catch {
    return
  }
  saving.value = true
  try {
    const saved = await toggleCopilotSecurityKillSwitch({ enabled: turningOn, reason: reason || undefined })
    applySaved(saved)
    ElMessage.success(turningOn ? '已紧急停机' : '已解除停机')
  } catch (error) {
    reportError(error, '停机开关操作失败，请重试')
  } finally {
    saving.value = false
  }
}

async function onSavePolicy(): Promise<void> {
  const patch = buildPatch()
  if (!Object.keys(patch).length) {
    ElMessage.info('没有改动，无需保存')
    return
  }
  saving.value = true
  try {
    const saved = await updateCopilotSecurityPolicy(patch)
    applySaved(saved)
    ElMessage.success(saved.changed.length ? `已保存：${saved.changed.join('、')}` : '已保存，策略无变化')
  } catch (error) {
    reportError(error, '保存策略失败，请重试')
  } finally {
    saving.value = false
  }
}

async function onScan(): Promise<void> {
  if (!scanText.value.trim()) {
    ElMessage.warning('请先输入要演练的文本')
    return
  }
  scanning.value = true
  try {
    scanResult.value = await scanCopilotSecurityText({ text: scanText.value })
  } catch (error) {
    reportError(error, '演练失败，请重试')
  } finally {
    scanning.value = false
  }
}

onMounted(() => {
  void loadCenter()
  void loadEvents()
})
</script>

<style scoped>
.copilot-security-page { min-width: 0; }
.kill-banner { margin-bottom: 12px; }
.page-header { margin-bottom: 12px; }
.page-header h2 { margin: 0; }
.page-sub { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.section-card { margin-top: 12px; }
.posture-head { display: flex; align-items: center; justify-content: space-between; gap: 24px; }
.posture-score { display: flex; align-items: center; gap: 16px; flex: 1; min-width: 0; }
.score-number { font-size: 40px; font-weight: 700; line-height: 1; }
.score-bar { flex: 1; min-width: 120px; }
.posture-actions { display: flex; gap: 8px; flex-shrink: 0; }
.posture-meta { margin: 10px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.preset-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.preset-card {
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  padding: 12px; border: 1px solid var(--el-border-color); border-radius: 8px;
  background: var(--el-fill-color-blank); cursor: pointer; text-align: left;
}
.preset-card.is-current { border-color: var(--el-color-success); background: var(--el-color-success-light-9); }
.preset-card:disabled { cursor: not-allowed; opacity: 0.6; }
.preset-code { color: var(--el-text-color-secondary); font-size: 12px; }
.policy-head-controls { display: inline-flex; align-items: center; gap: 12px; }
.policy-enabled-switch { display: inline-flex; align-items: center; gap: 6px; }
.switch-label { color: var(--el-text-color-secondary); font-size: 13px; }
.card-head-row { display: flex; align-items: center; justify-content: space-between; }
.gap-row { margin-bottom: 8px; }
.passed-list { margin: 0; padding-left: 18px; }
.drill-note { margin: 0 0 8px; color: var(--el-text-color-secondary); font-size: 13px; }
.drill-actions { margin: 8px 0; }
.redacted-text, .event-detail {
  white-space: pre-wrap; word-break: break-all; margin: 0;
  background: var(--el-fill-color-light); padding: 8px; border-radius: 4px; font-size: 12px;
}
.filter-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 12px; }
@media (max-width: 860px) {
  .posture-head { flex-direction: column; align-items: stretch; }
  .preset-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
