<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ArrowRight, CircleCheck, CopyDocument, Lock, Refresh } from '@element-plus/icons-vue'
import {
  getAutomaticBlocking,
  updateAutomaticBlocking,
  releaseAutomaticBlock,
  type AutomaticBlockingSnapshot,
  type AutomaticBlockingPolicyInput,
  type AutomaticBlockEntry,
} from '@/api/adminSecurityCenter'

const emit = defineEmits<{
  status: [value: { snapshot: AutomaticBlockingSnapshot | null; loading: boolean; error: string }]
  changed: []
}>()
const snapshot = ref<AutomaticBlockingSnapshot | null>(null)
const loading = ref(true)
const loadError = ref('')
const actionError = ref('')
const saving = ref(false)
const releasingIp = ref<string | null>(null)
const unconfirmedSave = ref(false)
const saveNote = ref('')
const clock = ref(Date.now())
const readAt = ref<string | null>(null)
const allowlistText = ref('')
const draft = reactive<AutomaticBlockingPolicyInput>({
  enabled: false, ai_anomaly_enabled: false, duration_seconds: 900, window_seconds: 300, ssh_threshold: 20, web_threshold: 30, allowlist_cidrs: [], auto_escalate: false,
})
let generation = 0
let expiryTimer: ReturnType<typeof setTimeout> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
const expiryChecks = new Set<string>()

const stateLabel = computed(() => {
  if (loading.value) return '正在读取自动封禁状态…'
  if (loadError.value || !snapshot.value) return '自动封禁状态未知'
  if (!snapshot.value.available) return '执行器不可用'
  if (!snapshot.value.verified || snapshot.value.outcome_unknown) return '自动封禁执行状态待核验'
  return snapshot.value.enabled ? '自动封禁已启用' : '自动封禁已关闭'
})
const records = computed(() => {
  const seen = new Set<string>()
  return [...(snapshot.value?.active_blocks ?? []), ...(snapshot.value?.recent_blocks ?? [])].filter((item) => {
    if (seen.has(item.id)) return false
    seen.add(item.id)
    return true
  })
})
function reachedExpiry(entry: AutomaticBlockEntry): boolean {
  const deadline = Date.parse(entry.expires_at || '')
  return entry.status === 'active' && Number.isFinite(deadline) && deadline <= clock.value
}
const enabledCount = computed(() => snapshot.value?.active_blocks.filter((item) => item.status === 'active' && !reachedExpiry(item)).length ?? 0)
const pendingExpiryCount = computed(() => snapshot.value?.active_blocks.filter(reachedExpiry).length ?? 0)
const verified = computed(() => Boolean(snapshot.value?.available && snapshot.value.verified && !snapshot.value.outcome_unknown && !loadError.value && !loading.value))
const busy = computed(() => saving.value || releasingIp.value !== null)
const controlDisabled = computed(() => loading.value || busy.value || !snapshot.value || Boolean(loadError.value) || unconfirmedSave.value)
const draftDirty = computed(() => snapshot.value ? !policiesMatch({ ...draft, allowlist_cidrs: normalizedAllowlist() }, snapshot.value.policy) : false)
const saveDisabled = computed(() => controlDisabled.value || !draftDirty.value || (!verified.value && draft.enabled))
const protectedSources = computed(() => {
  const grouped = new Map<string, Set<string>>()
  for (const source of snapshot.value?.protected_sources ?? []) {
    if (!grouped.has(source.cidr)) grouped.set(source.cidr, new Set())
    grouped.get(source.cidr)!.add(source.reason)
  }
  return Array.from(grouped, ([cidr, reasons]) => ({ cidr, reasons: [...reasons] }))
})
const ruleDisabledReason = computed(() => {
  if (loading.value) return '正在读取最新状态，请稍候。'
  if (busy.value) return '正在提交并核验处置，请稍候。'
  if (loadError.value || !snapshot.value) return '状态读取失败，刷新核验后可编辑策略。'
  if (unconfirmedSave.value) return '上次保存尚未确认，刷新核验后可继续编辑。'
  if (!snapshot.value.available) return snapshot.value.policy.enabled ? '执行器当前不可用，可关闭当前策略。' : '执行器当前不可用，暂时无法开启自动封禁。'
  if (!verified.value) return snapshot.value.policy.enabled ? '执行回执尚未确认，可关闭当前策略。' : '执行回执尚未确认，暂时无法开启自动封禁。'
  return ''
})
const aiDisabledReason = computed(() => ruleDisabledReason.value || (!draft.enabled ? '启用规则自动封禁后，可开启小菱研判。' : ''))

function policiesMatch(left: AutomaticBlockingPolicyInput, right: AutomaticBlockingPolicyInput): boolean {
  return left.enabled === right.enabled && left.ai_anomaly_enabled === right.ai_anomaly_enabled
    && left.duration_seconds === right.duration_seconds && left.window_seconds === right.window_seconds
    && left.ssh_threshold === right.ssh_threshold && left.web_threshold === right.web_threshold
    && left.auto_escalate === right.auto_escalate
    && [...left.allowlist_cidrs].sort().join(',') === [...right.allowlist_cidrs].sort().join(',')
}

function errorMessage(error: unknown): string {
  const value = error as { message?: string; response?: { data?: { message?: string } } } | null
  return value?.response?.data?.message || value?.message || '请求失败，请稍后重试。'
}

function statusChanged(): void {
  emit('status', { snapshot: loadError.value ? null : snapshot.value, loading: loading.value || busy.value, error: loadError.value })
}

function syncDraft(): void {
  if (!snapshot.value) return
  const policy = snapshot.value.policy
  Object.assign(draft, {
    enabled: policy.enabled, ai_anomaly_enabled: policy.ai_anomaly_enabled, duration_seconds: policy.duration_seconds, window_seconds: policy.window_seconds,
    ssh_threshold: policy.ssh_threshold, web_threshold: policy.web_threshold, allowlist_cidrs: [...policy.allowlist_cidrs],
    auto_escalate: policy.auto_escalate,
  })
  allowlistText.value = policy.allowlist_cidrs.join('\n')
}

async function refresh(sync = true): Promise<boolean> {
  const current = ++generation
  loading.value = true
  if (expiryTimer) clearTimeout(expiryTimer)
  loadError.value = ''
  statusChanged()
  try {
    const result = await getAutomaticBlocking()
    if (current !== generation) return false
    const shouldSyncDraft = sync && !draftDirty.value
    snapshot.value = result
    clock.value = Date.now()
    readAt.value = new Date(clock.value).toISOString()
    unconfirmedSave.value = false
    if (shouldSyncDraft) syncDraft()
    return true
  } catch (error) {
    if (current === generation) loadError.value = errorMessage(error)
    return false
  } finally {
    if (current === generation) {
      loading.value = false
      statusChanged()
      if (!loadError.value) scheduleExpiryCheck()
    }
  }
}

function scheduleExpiryCheck(): void {
  if (expiryTimer) clearTimeout(expiryTimer)
  const next = snapshot.value?.active_blocks.map((entry) => ({
    deadline: Date.parse(entry.expires_at || ''), key: `${entry.id}:${entry.expires_at}`,
  })).filter((entry) => Number.isFinite(entry.deadline) && !expiryChecks.has(entry.key))
    .sort((left, right) => left.deadline - right.deadline)[0]
  if (!next) return
  const check = () => {
    clock.value = Date.now()
    if (busy.value || loading.value) {
      expiryTimer = setTimeout(check, 1_000)
      return
    }
    expiryChecks.add(next.key)
    void refresh()
  }
  expiryTimer = setTimeout(check, Math.min(Math.max(next.deadline - Date.now() + 100, 100), 2_147_483_647))
}

function normalizedAllowlist(): string[] {
  return [...new Set(allowlistText.value.split(/[\n,]/).map((cidr) => cidr.trim()).filter(Boolean))]
}

function resetDraft(): void {
  if (controlDisabled.value) return
  syncDraft()
  actionError.value = ''
  saveNote.value = ''
}

async function copySource(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('来源地址已复制。')
  } catch {
    ElMessage.warning('复制失败，请选择完整地址手动复制。')
  }
}

async function save(): Promise<void> {
  if (saveDisabled.value) return
  const requested = { ...draft, allowlist_cidrs: normalizedAllowlist() }
  if (requested.allowlist_cidrs.length > 32) {
    actionError.value = '白名单最多保存 32 条 CIDR。'
    return
  }
  actionError.value = ''
  saveNote.value = ''
  saving.value = true
  unconfirmedSave.value = true
  statusChanged()
  try {
    const submitted = await updateAutomaticBlocking(requested)
    const confirmed = await refresh(false)
    if (!confirmed) {
      saveNote.value = '策略提交已返回，重新读取失败。请刷新核验。'
      ElMessage.warning('策略提交已返回，但重新读取失败；请刷新核验后再保存。')
      return
    }
    const actual = snapshot.value?.policy
    const matches = actual && requested.enabled === actual.enabled && requested.ai_anomaly_enabled === actual.ai_anomaly_enabled
      && requested.duration_seconds === actual.duration_seconds && requested.window_seconds === actual.window_seconds
      && requested.ssh_threshold === actual.ssh_threshold && requested.web_threshold === actual.web_threshold
      && requested.auto_escalate === actual.auto_escalate
      && [...(submitted.policy?.allowlist_cidrs ?? requested.allowlist_cidrs)].sort().join(',') === [...actual.allowlist_cidrs].sort().join(',')
    if (!verified.value || !submitted.available || !submitted.verified || submitted.outcome_unknown) {
      saveNote.value = '策略已回读，执行结果尚未确认。请刷新核验。'
      ElMessage.warning('策略已回读，但执行器回执未确认；自动封禁能力仍需核验。')
    } else if (matches) {
      syncDraft()
      saveNote.value = '策略已回读确认，变更已记录。'
      ElMessage.success('自动封禁策略已回读确认，变更已记录审计。')
    } else {
      saveNote.value = '服务端策略与提交内容不同，当前修改已保留。'
      ElMessage.warning('服务端当前策略与提交内容不同，请核验当前状态后调整。')
    }
    emit('changed')
  } catch (error) {
    actionError.value = `策略保存未确认：${errorMessage(error)}`
    ElMessage.error(actionError.value)
    await refresh(false)
  } finally {
    saving.value = false
    statusChanged()
  }
}

function formatTime(value: string | null): string {
  if (!value) return '尚无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间格式异常' : date.toLocaleString('zh-CN', { hour12: false })
}

function recordStatus(entry: AutomaticBlockEntry): string {
  if (!verified.value && (entry.status === 'active' || entry.status === 'released' || entry.status === 'expired')) return '状态待核验'
  if (reachedExpiry(entry)) return '到期状态待核验'
  return ({ active: '已生效（临时）', expired: '已到期', released: '已解封', failed: '执行失败', unknown: '状态待核验' } as Record<string, string>)[entry.status] || '状态待核验'
}

function ruleLabel(entry: AutomaticBlockEntry): string {
  if (entry.source === 'xiaoling_anomaly') {
    return entry.rule === 'ssh_failed_password' ? '小菱异常研判 · SSH' : entry.rule === 'web_sensitive_probe' ? '小菱异常研判 · Web' : '小菱异常研判'
  }
  return ({
    ssh_failed_password: 'SSH 重复认证失败', web_sensitive_probe: 'Web 多目标敏感路径探测',
    ai_ssh_anomaly: '小菱异常研判 · SSH', ai_web_anomaly: '小菱异常研判 · Web',
  } as Record<string, string>)[entry.rule] || entry.rule
}

function scopeLabel(value: string): string {
  return value === 'host_ingress_and_docker_web' ? '宿主机入站 + Prism Docker 80/443' : value
}

function executorError(value: string | Record<string, unknown>): string {
  if (typeof value === 'string') return value
  return typeof value.message === 'string' ? value.message : typeof value.error === 'string' ? value.error : JSON.stringify(value)
}

async function release(entry: AutomaticBlockEntry): Promise<void> {
  if (busy.value || !verified.value || entry.status !== 'active') return
  actionError.value = ''
  releasingIp.value = entry.ip
  statusChanged()
  try {
    const answer = await ElMessageBox.prompt(`来源 ${entry.ip} 当前处于临时封禁。请输入核验结论，解封原因会写入审计。`, '手动解封来源', {
      confirmButtonText: '提交解封', cancelButtonText: '取消', inputType: 'textarea', inputPlaceholder: '说明为何解除此来源的封禁',
      inputValidator: (value: string) => Boolean(value.trim()) && value.trim().length <= 200 || '请输入 1 至 200 个字符的解封原因',
    })
    await releaseAutomaticBlock({ ip: entry.ip, reason: answer.value.trim() })
    const confirmed = await refresh()
    if (!confirmed) ElMessage.warning('解封请求已返回，但状态重新读取失败，请刷新核验。')
    else if (!verified.value) ElMessage.warning('执行器回执未确认，当前解封状态待核验。')
    else if (snapshot.value?.active_blocks.some((item) => item.ip === entry.ip && item.status === 'active')) {
      ElMessage.warning('该来源仍显示生效，尚不能确认解封成功，请查看执行回执。')
    } else if (snapshot.value?.recent_blocks.some((item) => item.ip === entry.ip && (item.status === 'released' || item.status === 'expired'))) {
      ElMessage.success(`来源 ${entry.ip} 已核验解封，处置记录已保留。`)
    } else ElMessage.warning('未取得明确解封回执，当前状态待核验。')
    emit('changed')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      actionError.value = `解封未确认：${errorMessage(error)}`
      ElMessage.error(actionError.value)
      await refresh(false)
    }
  } finally {
    releasingIp.value = null
    statusChanged()
  }
}

onMounted(() => {
  void refresh()
  clockTimer = setInterval(() => { clock.value = Date.now() }, 30_000)
})
onUnmounted(() => {
  generation += 1
  if (expiryTimer) clearTimeout(expiryTimer)
  if (clockTimer) clearInterval(clockTimer)
})
defineExpose({ refresh: () => busy.value ? Promise.resolve(false) : refresh() })
</script>

<template>
  <section class="blocking-panel" aria-labelledby="blocking-title" :aria-busy="loading || busy">
    <header class="blocking-heading">
      <div class="blocking-title-group">
        <div class="mini-shield" aria-hidden="true"><svg viewBox="0 0 36 40"><path d="M18 3 31 8v10c0 9-5 15-13 19C10 33 5 27 5 18V8L18 3Z" /><path d="m12 20 4 4 8-9" /></svg></div>
        <div><h3 id="blocking-title">临时自动封禁</h3><p>配置来源 IP 的临时限制与保护范围。</p></div>
      </div>
      <button class="blocking-button refresh-button" type="button" :disabled="busy" :aria-busy="loading" @click="refresh()"><Refresh aria-hidden="true" />刷新封禁状态</button>
    </header>

    <div class="blocking-status-row" role="status" aria-live="polite">
      <span class="blocking-state" :class="{ enabled: verified && snapshot?.enabled, warning: !loading && !loadError && snapshot && (!snapshot.available || !snapshot.verified || snapshot.outcome_unknown) }"><CircleCheck v-if="verified && snapshot?.enabled" aria-hidden="true" /><Lock v-else aria-hidden="true" />{{ stateLabel }}</span>
      <span>{{ verified ? `当前确认生效 ${enabledCount} 个来源` : '生效数量待核验' }}</span>
      <span v-if="verified && pendingExpiryCount" class="expiry-pending">{{ pendingExpiryCount }} 个来源到期状态待核验</span>
    </div>
    <div class="blocking-read-times"><span>最近规则评估：{{ formatTime(snapshot?.last_evaluated_at || null) }}</span><span>状态读取：{{ formatTime(readAt) }}</span></div>
    <p v-if="loadError" class="blocking-error" role="alert">自动封禁状态读取失败：{{ loadError }}。请刷新核验；当前状态未知。</p>
    <p v-if="unconfirmedSave && !saving" class="blocking-warning" role="status">上次保存结果尚未确认，请先刷新核验后再提交。</p>
    <p v-if="!loading && snapshot?.outcome_unknown" class="blocking-warning" role="status">执行结果尚未确认，当前状态待核验。请刷新封禁状态。</p>
    <p v-if="actionError" class="blocking-error" role="alert">{{ actionError }}</p>
    <details v-if="!loading && !loadError && snapshot?.errors.length" class="blocking-details executor-diagnostics">
      <summary><ArrowRight aria-hidden="true" /><span>执行器诊断</span><small>{{ snapshot.errors.length }} 项需要核验</small></summary>
      <ul class="executor-errors"><li v-for="(error, index) in snapshot.errors" :key="index">{{ executorError(error) }}</li></ul>
    </details>

    <form class="blocking-form" @submit.prevent="save">
      <div class="policy-switches">
        <div class="enable-field">
          <div class="switch-heading"><span><strong>规则自动封禁</strong><small>达到规则阈值后临时限制单个来源</small></span><label class="switch-control" for="blocking-enabled"><input id="blocking-enabled" v-model="draft.enabled" type="checkbox" :disabled="controlDisabled || (!verified && !draft.enabled)" aria-describedby="rule-switch-help"><span class="switch-track" aria-hidden="true" /><span class="sr-only">启用规则自动封禁</span></label></div>
          <p id="rule-switch-help" class="field-help">{{ ruleDisabledReason || '关闭后停止新增封禁，并立即请求解除已有封禁；结果需执行回执确认。' }}</p>
        </div>
        <div class="enable-field ai-field">
          <div class="switch-heading"><span><strong>小菱主动研判</strong><small>对可信日志候选进行异常研判</small></span><label class="switch-control" for="blocking-ai-enabled"><input id="blocking-ai-enabled" v-model="draft.ai_anomaly_enabled" type="checkbox" :disabled="controlDisabled || !verified || !draft.enabled" aria-describedby="ai-switch-help"><span class="switch-track" aria-hidden="true" /><span class="sr-only">启用小菱主动研判异常</span></label></div>
          <p id="ai-switch-help" class="field-help">{{ aiDisabledReason || '最多封禁 2 分钟；每天最多 24 次模型研判。' }}</p>
        </div>
      </div>

      <div class="policy-fields">
        <label class="number-field" for="blocking-duration"><span>封禁时长</span><div><input id="blocking-duration" v-model.number="draft.duration_seconds" type="number" min="60" max="3600" step="1" required :disabled="controlDisabled" aria-describedby="blocking-duration-help"><em>秒</em></div><small id="blocking-duration-help">60 – 3600 秒</small></label>
        <label class="toggle-field" for="blocking-escalate"><span>重复攻击自动延长</span><div>
          <input id="blocking-escalate" v-model="draft.auto_escalate" type="checkbox" :disabled="controlDisabled" aria-describedby="blocking-escalate-help">
          <em>{{ draft.auto_escalate ? '已开启' : '已关闭' }}</em>
        </div><small id="blocking-escalate-help">同一来源 24 小时内再次触发时，租约按 4 倍递增，最长 6 小时；关闭时每次都用上面的固定时长。</small></label>
        <label class="number-field" for="blocking-window"><span>规则观察窗口</span><div><input id="blocking-window" v-model.number="draft.window_seconds" type="number" min="60" max="900" step="1" required :disabled="controlDisabled" aria-describedby="blocking-window-help"><em>秒</em></div><small id="blocking-window-help">60 – 900 秒</small></label>
        <label class="number-field" for="blocking-ssh"><span>SSH 失败阈值</span><div><input id="blocking-ssh" v-model.number="draft.ssh_threshold" type="number" min="20" max="200" step="1" required :disabled="controlDisabled" aria-describedby="blocking-ssh-help"><em>次</em></div><small id="blocking-ssh-help">20 – 200 次</small></label>
        <label class="number-field" for="blocking-web"><span>Web 探测阈值</span><div><input id="blocking-web" v-model.number="draft.web_threshold" type="number" min="30" max="500" step="1" required :disabled="controlDisabled" aria-describedby="blocking-web-help"><em>次</em></div><small id="blocking-web-help">30 – 500 次</small></label>
      </div>
      <label class="allowlist-field" for="blocking-allowlist"><span>额外保护白名单</span><textarea id="blocking-allowlist" v-model="allowlistText" rows="3" placeholder="每行一个 IP/CIDR，例如 198.51.100.8/32" :disabled="controlDisabled" aria-describedby="blocking-allowlist-help" /><small id="blocking-allowlist-help">最多 32 条；IPv4 前缀至少 /24，IPv6 至少 /64。系统保护来源始终优先。</small></label>
      <div class="policy-actions">
        <p class="draft-status" :class="{ dirty: draftDirty }" role="status" aria-live="polite">{{ saving ? '正在提交策略并重新读取核验…' : loading ? '正在读取策略…' : loadError || !snapshot ? '策略未能读取，暂时无法保存' : unconfirmedSave ? '保存结果待核验' : draftDirty ? '未保存的修改 · 刷新会保留当前草稿' : saveNote || '当前策略已同步，暂无修改' }}</p>
        <div class="policy-action-buttons"><button class="blocking-button reset-button" type="button" :disabled="!draftDirty || controlDisabled" @click="resetDraft">重置修改</button><button class="blocking-button save-button" type="submit" :disabled="saveDisabled">{{ saving ? '提交并核验中…' : '保存自动封禁策略' }}</button></div>
      </div>
      <p v-if="saveNote && draftDirty && !saving" class="save-feedback" role="status">{{ saveNote }}</p>
    </form>

    <div class="blocking-information">
      <details class="blocking-details rules-details">
        <summary><ArrowRight aria-hidden="true" /><span>规则与保护范围</span><small>查看规则条件</small></summary>
        <ul class="rule-notes">
          <li><strong>SSH：</strong>短窗口内重复出现 Failed password，达到阈值后才进入封禁评估。规则封禁最长 15 分钟。</li>
          <li><strong>Web：</strong>敏感路径探测达到阈值，且涉及多个不同目标才进入封禁评估；普通 403 不触发封禁。</li>
          <li><strong>作用范围：</strong>宿主机入站（INPUT）与 Prism Docker 的 80/443 流量。</li>
          <li><strong>小菱研判：</strong>需启用规则自动封禁，仅研判可信日志候选，封禁最多 2 分钟；每天最多 24 次模型研判。</li>
          <li><strong>研判门槛：</strong>SSH 认证失败至少 10 次，或 Web 探测至少 10 次且涉及 3 个敏感目标。实际封禁前由执行器重新核验证据与保护名单，不能任意封 IP、网段或永久封禁。</li>
          <li><strong>执行依据：</strong>命中或研判建议均不代表生效，以执行器回执显示结果。</li>
        </ul>
        <p class="family-support"><span>IPv4：{{ verified && snapshot?.family_support ? snapshot.family_support.ipv4 ? '已核验支持' : '不支持' : '待核验' }}</span><span>IPv6：{{ verified && snapshot?.family_support ? snapshot.family_support.ipv6 ? '已核验支持' : '不支持' : '待核验' }}</span></p>
      </details>
      <details class="blocking-details protected-details">
        <summary><ArrowRight aria-hidden="true" /><span>固定保护来源</span><small>{{ !snapshot || loadError ? '待核验' : `${protectedSources.length} 个来源` }}</small></summary>
        <p v-if="!snapshot || loadError" class="blocking-muted">保护名单尚未核验。</p>
        <p v-else-if="!protectedSources.length" class="blocking-muted">当前回执未返回保护来源，请核验执行器配置。</p>
        <ul v-else class="protected-list"><li v-for="source in protectedSources" :key="source.cidr"><div class="protected-source-heading"><code>{{ source.cidr }}</code><span class="protected-tag">系统保护</span><button class="copy-button" type="button" :aria-label="`复制保护来源 ${source.cidr}`" @click="copySource(source.cidr)"><CopyDocument aria-hidden="true" /></button></div><ul class="protected-reasons"><li v-for="reason in source.reasons" :key="reason">{{ reason }}</li></ul></li></ul>
      </details>
    </div>

    <section class="block-record-section" aria-labelledby="block-record-title">
      <div class="block-record-heading"><div><h4 id="block-record-title">封禁与解封记录</h4><p>生效状态依据执行回执</p></div><span>{{ loading || loadError ? '待核验' : `${records.length} 条记录` }}</span></div>
      <p v-if="loading" class="records-empty" role="status">正在核验封禁记录…</p>
      <p v-else-if="loadError" class="records-empty">当前封禁记录未能读取，请刷新核验。</p>
      <p v-else-if="!records.length" class="records-empty">暂无匹配的封禁记录。</p>
      <template v-else>
        <div class="record-columns" aria-hidden="true"><span>来源与处置记录</span><span>执行状态</span><span>操作</span></div>
        <ul class="block-records">
          <li v-for="entry in records" :key="entry.id" class="block-record">
            <div class="block-record-content"><div class="block-source"><code>{{ entry.ip }}</code><button class="copy-button" type="button" :aria-label="`复制来源 ${entry.ip}`" @click="copySource(entry.ip)"><CopyDocument aria-hidden="true" /></button></div><p class="block-record-rule"><strong>{{ ruleLabel(entry) }}</strong><span>证据 {{ entry.evidence_count }} 次</span></p><dl><div><dt>作用范围</dt><dd>{{ scopeLabel(entry.scope) }}</dd></div><div><dt>生效记录</dt><dd>{{ formatTime(entry.started_at) }}</dd></div><div><dt>到期时间</dt><dd>{{ formatTime(entry.expires_at) }}</dd></div><div v-if="entry.released_at"><dt>解封记录</dt><dd>{{ formatTime(entry.released_at) }}</dd></div></dl><p v-if="entry.reason" class="record-reason"><span>依据 / 处置</span>{{ entry.reason }}</p></div>
            <div class="record-status-cell"><span class="record-state" :class="`record-${(verified && !reachedExpiry(entry)) || entry.status === 'failed' ? entry.status : 'unknown'}`">{{ recordStatus(entry) }}</span></div>
            <div class="record-action-cell"><button v-if="entry.status === 'active'" class="blocking-button release-button" type="button" :disabled="busy || !verified" @click="release(entry)">{{ releasingIp === entry.ip ? '解封核验中…' : '手动解封' }}</button><span v-else class="record-no-action">无需操作</span><small v-if="entry.status === 'active' && !verified">状态核验后可解封</small></div>
          </li>
        </ul>
      </template>
    </section>
  </section>
</template>

<style scoped lang="scss">
.blocking-panel { min-width: 0; padding: var(--sp-5); border: 1px solid var(--surface-border); border-radius: var(--r-lg); background: var(--color-bg-card); color: var(--gray-800); box-shadow: var(--shadow-1); font-size: 14px; }
.blocking-heading, .blocking-title-group, .blocking-status-row, .block-record-heading { display: flex; align-items: center; gap: var(--sp-3); }
.blocking-heading { justify-content: space-between; align-items: flex-end; gap: var(--sp-4); }
.blocking-title-group { min-width: 0; align-items: center; }
.mini-shield { display: grid; width: 44px; height: 48px; flex: 0 0 44px; place-items: center; border-radius: var(--r-lg); background: var(--brand-50); }
.mini-shield svg { width: 29px; fill: rgba(91,88,232,.12); stroke: var(--brand-500); stroke-width: 2.3; stroke-linecap: round; stroke-linejoin: round; }
h3, h4, p { margin: 0; }
h3 { font-size: 18px; line-height: 1.5; }
h4 { font-size: 16px; line-height: 1.5; }
.blocking-heading p { margin-top: 3px; color: var(--gray-500); font-size: 14px; line-height: 1.6; }
.blocking-button, .copy-button { display: inline-flex; box-sizing: border-box; min-height: 44px; justify-content: center; align-items: center; gap: 8px; padding: 8px 14px; border: 1px solid var(--gray-200); border-radius: var(--r-md); color: var(--gray-700); background: var(--color-bg-card); font: 600 14px/1.5 var(--font-sans); cursor: pointer; transition: border-color .15s, background-color .15s; }
.blocking-button svg, .copy-button svg { width: 16px; height: 16px; flex: 0 0 16px; }
.blocking-button:hover:not(:disabled), .copy-button:hover { border-color: var(--brand-300); background: var(--brand-50); }
.blocking-button:disabled { opacity: .55; cursor: not-allowed; }
button:focus-visible, input:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 3px solid var(--brand-200); outline-offset: 3px; }
.refresh-button { flex: 0 0 auto; }
.blocking-status-row { flex-wrap: wrap; gap: 8px 16px; margin: 18px 0 8px; color: var(--gray-600); font-size: 14px; line-height: 1.6; }
.blocking-state { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 999px; color: var(--gray-600); background: var(--gray-100); font-size: 12px; font-weight: 700; }
.blocking-state svg { width: 15px; height: 15px; }
.blocking-state.enabled, .record-active { color: #187b73; background: #e9f8f4; }
.blocking-state.warning, .record-unknown { color: #90601e; background: var(--color-warning-light); }
.expiry-pending { color: #90601e; }
.blocking-read-times { display: flex; flex-wrap: wrap; gap: 4px 20px; color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.blocking-error, .blocking-warning { margin: 12px 0; padding: 12px 14px; border: 1px solid #f2c8d0; border-radius: var(--r-md); color: #a4374b; background: var(--color-danger-light); font-size: 14px; line-height: 1.6; overflow-wrap: anywhere; }
.blocking-warning { color: #90601e; border-color: #f1dfb9; background: var(--color-warning-light); }
.blocking-form { display: grid; min-width: 0; gap: var(--sp-4); margin-top: var(--sp-5); }
.policy-switches { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); }
.enable-field { min-width: 0; padding: var(--sp-4); border: 1px solid var(--gray-200); border-radius: var(--r-lg); background: var(--gray-50); }
.ai-field { border-color: var(--brand-100); background: var(--brand-50); }
.switch-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
.switch-heading > span { min-width: 0; }
.switch-heading strong { display: block; font-size: 14px; line-height: 1.6; }
.switch-heading small { display: block; margin-top: 2px; color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.switch-control { position: relative; display: inline-flex; flex: 0 0 48px; min-width: 48px; min-height: 44px; align-items: center; justify-content: center; cursor: pointer; }
.switch-control input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.switch-track { width: 40px; height: 24px; box-sizing: border-box; border: 1px solid var(--gray-300); border-radius: 999px; background: var(--gray-300); pointer-events: none; transition: background-color .15s; }
.switch-track::after { content: ''; display: block; width: 18px; height: 18px; margin: 2px; border-radius: 50%; background: white; box-shadow: var(--shadow-1); transition: transform .15s; }
.switch-control input:checked + .switch-track { border-color: var(--brand-500); background: var(--brand-500); }
.switch-control input:checked + .switch-track::after { transform: translateX(16px); }
.switch-control input:disabled { cursor: not-allowed; }
.switch-control input:disabled + .switch-track { opacity: .5; }
.switch-control input:focus-visible + .switch-track { outline: 3px solid var(--brand-200); outline-offset: 3px; }
.field-help { margin-top: 8px; color: var(--gray-600); font-size: 12px; line-height: 1.7; }
.policy-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); align-items: end; gap: var(--sp-4); }
.number-field, .allowlist-field { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.number-field > span, .allowlist-field > span { font-size: 14px; line-height: 1.6; font-weight: 600; }
.number-field > div { display: flex; align-items: center; min-width: 0; border: 1px solid var(--gray-200); border-radius: var(--r-md); background: var(--color-bg-card); }
.number-field input { box-sizing: border-box; width: 100%; min-width: 0; min-height: 44px; padding: 8px 10px; border: 0; border-radius: var(--r-md); color: var(--gray-800); background: transparent; font: 14px/1.5 var(--font-mono); }
.number-field em { flex: 0 0 auto; padding-right: 10px; color: var(--gray-500); font-size: 12px; font-style: normal; }
.number-field small, .allowlist-field small { color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.number-field input:disabled, .allowlist-field textarea:disabled { cursor: not-allowed; background: var(--gray-50); }
.allowlist-field textarea { box-sizing: border-box; width: 100%; min-width: 0; min-height: 88px; padding: 10px 12px; border: 1px solid var(--gray-200); border-radius: var(--r-md); color: var(--gray-700); background: var(--color-bg-card); font: 14px/1.7 var(--font-mono); resize: vertical; overflow-wrap: anywhere; }
.policy-actions { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px 20px; padding-top: 4px; }
.draft-status { color: var(--gray-500); font-size: 12px; line-height: 1.7; padding-bottom: 10px; overflow-wrap: anywhere; }
.draft-status.dirty { color: var(--brand-600); }
.save-feedback { color: #90601e; font-size: 12px; line-height: 1.7; }
.policy-action-buttons { display: flex; flex: 0 0 auto; gap: var(--sp-2); }
.save-button { color: white; border-color: var(--brand-500); background: var(--brand-500); }
.save-button:hover:not(:disabled) { color: white; border-color: var(--brand-600); background: var(--brand-600); }
.blocking-information { display: grid; gap: var(--sp-3); margin-top: var(--sp-5); }
.blocking-details { min-width: 0; border: 1px solid var(--gray-200); border-radius: var(--r-md); background: var(--gray-50); }
.blocking-details summary { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 7px 14px; box-sizing: border-box; list-style: none; cursor: pointer; font-size: 14px; font-weight: 600; line-height: 1.6; }
.blocking-details summary::-webkit-details-marker { display: none; }
.blocking-details summary > svg { width: 14px; height: 14px; flex: 0 0 14px; color: var(--gray-500); transition: transform .15s; }
.blocking-details[open] summary > svg { transform: rotate(90deg); }
.blocking-details summary > span { min-width: 0; }
.blocking-details summary small { margin-left: auto; color: var(--gray-500); font-size: 12px; line-height: 1.6; font-weight: 400; text-align: right; }
.executor-diagnostics { margin-top: 12px; border-color: #f1dfb9; background: var(--color-warning-light); }
.executor-errors { margin: 0; padding: 0 18px 14px 36px; color: var(--gray-600); font: 12px/1.7 var(--font-mono); overflow-wrap: anywhere; }
.executor-errors li + li { margin-top: 8px; }
.rule-notes { display: grid; gap: 8px; margin: 0; padding: 4px 18px 14px 34px; color: var(--gray-600); font-size: 14px; line-height: 1.7; }
.rule-notes strong { color: var(--gray-700); }
.family-support { display: flex; flex-wrap: wrap; gap: 6px 20px; padding: 0 18px 14px; color: var(--gray-600); font-size: 12px; line-height: 1.6; }
.protected-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 0; padding: 4px 14px 14px; list-style: none; }
.protected-list > li { min-width: 0; padding: 10px 12px; border: 1px solid var(--gray-200); border-radius: var(--r-md); background: var(--color-bg-card); }
.protected-source-heading { display: flex; align-items: center; gap: 6px; }
.protected-source-heading code { min-width: 0; flex: 1; overflow-wrap: anywhere; color: var(--gray-800); font: 14px/1.6 var(--font-mono); }
.protected-tag { flex: 0 0 auto; color: #187b73; font-size: 12px; }
.copy-button { width: 44px; flex: 0 0 44px; padding: 0; border-color: transparent; color: var(--gray-500); background: transparent; }
.protected-reasons { display: grid; gap: 4px; margin: 4px 0 0; padding-left: 16px; color: var(--gray-600); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.blocking-muted { padding: 4px 14px 14px; color: var(--gray-500); font-size: 14px; line-height: 1.6; }
.block-record-section { margin-top: var(--sp-5); padding-top: var(--sp-5); border-top: 1px solid var(--gray-200); }
.block-record-heading { justify-content: space-between; align-items: flex-end; gap: 12px; }
.block-record-heading p { margin-top: 4px; color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.block-record-heading > span { flex: 0 0 auto; padding-bottom: 2px; color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.records-empty { margin-top: 16px; padding: 24px 16px; border-radius: var(--r-md); background: var(--gray-50); color: var(--gray-500); font-size: 14px; line-height: 1.6; text-align: center; }
.record-columns, .block-record { display: grid; grid-template-columns: minmax(0, 1fr) 136px 136px; gap: 16px; }
.record-columns { margin-top: 16px; padding: 10px 16px; border-radius: var(--r-md) var(--r-md) 0 0; background: var(--gray-50); color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.block-records { margin: 0; padding: 0; list-style: none; }
.block-record { min-width: 0; padding: 12px 16px 16px; border: 1px solid var(--gray-200); border-top: 0; background: var(--color-bg-card); }
.block-record:last-child { border-radius: 0 0 var(--r-md) var(--r-md); }
.block-record-content { min-width: 0; }
.block-source { display: flex; align-items: center; gap: 8px; }
.block-source code { min-width: 0; overflow-wrap: anywhere; color: var(--gray-900); font: 600 14px/1.7 var(--font-mono); }
.block-record-rule { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; color: var(--gray-600); font-size: 14px; line-height: 1.7; }
.block-record-rule > span { color: var(--gray-500); font-size: 12px; }
.record-status-cell { padding-top: 10px; }
.record-state { display: inline-block; padding: 4px 8px; border-radius: 999px; color: var(--gray-600); background: var(--gray-100); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.record-active { color: #187b73; background: #e9f8f4; }
.record-failed { color: #a4374b; background: var(--color-danger-light); }
.record-unknown { color: #90601e; background: var(--color-warning-light); }
.block-record dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 16px; margin: 10px 0 0; }
.block-record dl > div { display: grid; grid-template-columns: 56px minmax(0, 1fr); gap: 8px; font-size: 12px; line-height: 1.7; }
.block-record dl > div:first-child { grid-column: 1 / -1; }
.block-record dt { color: var(--gray-500); }
.block-record dd { margin: 0; color: var(--gray-700); overflow-wrap: anywhere; }
.record-reason { margin-top: 8px; color: var(--gray-600); font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.record-reason > span { margin-right: 8px; color: var(--gray-500); font-size: 12px; }
.record-action-cell { display: flex; align-items: flex-start; flex-direction: column; gap: 6px; padding-top: 2px; }
.record-action-cell small { color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.record-no-action { display: inline-block; padding: 10px 0; color: var(--gray-500); font-size: 12px; line-height: 1.7; }
.release-button { width: 100%; color: var(--brand-600); border-color: var(--brand-100); background: var(--brand-50); padding-inline: 8px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
@media (max-width: 900px) {
  .policy-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .policy-actions { align-items: flex-start; flex-direction: column; }
  .draft-status { padding-bottom: 0; }
  .policy-action-buttons { align-self: flex-end; }
  .record-columns, .block-record { grid-template-columns: minmax(0, 1fr) 116px 120px; gap: 12px; }
  .block-record dl { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .blocking-panel { padding: var(--sp-4); }
  .blocking-heading { align-items: stretch; flex-direction: column; }
  .policy-switches, .protected-list { grid-template-columns: 1fr; }
  .blocking-status-row { align-items: flex-start; gap: 8px 12px; }
  .record-columns { display: none; }
  .block-records { display: grid; gap: 12px; margin-top: 16px; }
  .block-record { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; border: 1px solid var(--gray-200); border-radius: var(--r-md); padding: 12px; }
  .block-record:last-child { border-radius: var(--r-md); }
  .block-record-content { grid-column: 1 / -1; }
  .record-status-cell, .record-action-cell { padding-top: 0; }
  .record-status-cell { align-self: center; }
  .record-action-cell { align-items: stretch; }
  .policy-action-buttons { width: 100%; }
  .save-button { flex: 1; }
}
@media (max-width: 380px) {
  .blocking-panel { padding: 12px; }
  .policy-fields { gap: 12px; }
  .enable-field { padding: 12px; }
  .policy-action-buttons { flex-direction: column-reverse; }
  .blocking-details summary { padding-inline: 10px; gap: 6px; }
  .protected-source-heading { flex-wrap: wrap; }
  .protected-source-heading code { flex-basis: calc(100% - 50px); }
  .protected-tag { order: 1; }
  .block-record dl > div { grid-template-columns: 56px minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .blocking-button, .copy-button, .switch-track, .switch-track::after, .blocking-details summary > svg { transition: none; }
}
</style>
