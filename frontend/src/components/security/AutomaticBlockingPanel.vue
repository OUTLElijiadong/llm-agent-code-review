<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { CircleCheck, Lock, Refresh } from '@element-plus/icons-vue'
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
const clock = ref(Date.now())
const readAt = ref<string | null>(null)
const allowlistText = ref('')
const draft = reactive<AutomaticBlockingPolicyInput>({
  enabled: false, ai_anomaly_enabled: false, duration_seconds: 900, window_seconds: 300, ssh_threshold: 20, web_threshold: 30, allowlist_cidrs: [],
})
let generation = 0
let expiryTimer: ReturnType<typeof setTimeout> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
const expiryChecks = new Set<string>()

const stateLabel = computed(() => {
  if (loading.value) return '正在读取自动封禁状态…'
  if (loadError.value || !snapshot.value) return '自动封禁状态未知'
  if (!snapshot.value.available) return '执行器不可用'
  if (!snapshot.value.verified) return '自动封禁执行状态待核验'
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
const verified = computed(() => Boolean(snapshot.value?.available && snapshot.value.verified && !loadError.value && !loading.value))
const busy = computed(() => saving.value || releasingIp.value !== null)
const saveDisabled = computed(() => loading.value || busy.value || !snapshot.value || Boolean(loadError.value) || unconfirmedSave.value || (!verified.value && draft.enabled))

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
    snapshot.value = result
    clock.value = Date.now()
    readAt.value = new Date(clock.value).toISOString()
    unconfirmedSave.value = false
    if (sync) syncDraft()
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
    void refresh(false)
  }
  expiryTimer = setTimeout(check, Math.min(Math.max(next.deadline - Date.now() + 100, 100), 2_147_483_647))
}

function normalizedAllowlist(): string[] {
  return [...new Set(allowlistText.value.split(/[\n,]/).map((cidr) => cidr.trim()).filter(Boolean))]
}

async function save(): Promise<void> {
  if (saveDisabled.value) return
  const requested = { ...draft, allowlist_cidrs: normalizedAllowlist() }
  if (requested.allowlist_cidrs.length > 32) {
    actionError.value = '白名单最多保存 32 条 CIDR。'
    return
  }
  actionError.value = ''
  saving.value = true
  unconfirmedSave.value = true
  statusChanged()
  try {
    const submitted = await updateAutomaticBlocking(requested)
    const confirmed = await refresh(false)
    if (!confirmed) {
      ElMessage.warning('策略提交已返回，但重新读取失败；请刷新核验后再保存。')
      return
    }
    const actual = snapshot.value?.policy
    const matches = actual && requested.enabled === actual.enabled && requested.ai_anomaly_enabled === actual.ai_anomaly_enabled
      && requested.duration_seconds === actual.duration_seconds && requested.window_seconds === actual.window_seconds
      && requested.ssh_threshold === actual.ssh_threshold && requested.web_threshold === actual.web_threshold
      && [...(submitted.policy?.allowlist_cidrs ?? requested.allowlist_cidrs)].sort().join(',') === [...actual.allowlist_cidrs].sort().join(',')
    if (!snapshot.value?.available || !snapshot.value.verified || !submitted.available || !submitted.verified) {
      ElMessage.warning('策略已回读，但执行器回执未确认；自动封禁能力仍需核验。')
    } else if (matches) {
      syncDraft()
      ElMessage.success('自动封禁策略已回读确认，变更已记录审计。')
    } else {
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
    else if (!snapshot.value?.available || !snapshot.value.verified) ElMessage.warning('执行器回执未确认，当前解封状态待核验。')
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
        <div><span class="eyebrow">AUTOMATIC DEFENSE</span><h3 id="blocking-title">临时自动封禁</h3><p>基于可信日志与执行回执，对单个来源 IP 临时限制访问。规则封禁最长 15 分钟，小菱研判封禁最多 2 分钟。</p></div>
      </div>
      <button class="blocking-button refresh-button" type="button" :disabled="busy" @click="refresh()"><Refresh />刷新封禁状态</button>
    </header>
    <div class="blocking-status-row">
      <span class="blocking-state" :class="{ enabled: verified && snapshot?.enabled, warning: !loading && !loadError && snapshot && (!snapshot.available || !snapshot.verified) }"><CircleCheck v-if="verified && snapshot?.enabled" /><Lock v-else />{{ stateLabel }}</span>
      <span>{{ verified ? `当前确认生效 ${enabledCount} 个来源` : '生效数量待核验' }}</span>
      <span v-if="verified && pendingExpiryCount">{{ pendingExpiryCount }} 个来源到期状态待核验</span>
      <span>最近规则评估：{{ formatTime(snapshot?.last_evaluated_at || null) }}</span>
      <span>状态读取：{{ formatTime(readAt) }}</span>
    </div>
    <p v-if="loadError" class="blocking-error" role="alert">自动封禁状态读取失败：{{ loadError }}。请刷新核验；当前状态未知。</p>
    <p v-if="unconfirmedSave && !saving" class="blocking-warning" role="status">上次保存结果尚未确认，请先刷新对账后再提交。</p>
    <p v-if="actionError" class="blocking-error" role="alert">{{ actionError }}</p>
    <ul v-if="!loading && !loadError && snapshot?.errors.length" class="executor-errors"><li v-for="(error, index) in snapshot.errors" :key="index">{{ executorError(error) }}</li></ul>

    <div class="blocking-layout">
      <form class="blocking-form" @submit.prevent="save">
        <div class="enable-field full-field">
          <label><input v-model="draft.enabled" type="checkbox" :disabled="loading || busy || !snapshot || Boolean(loadError) || unconfirmedSave || (!verified && !draft.enabled)"><span>启用规则自动封禁</span></label>
          <small>关闭会立即发起解除当前自有封禁并停止新增处置；只有执行回执核验后才能确认解封。</small>
        </div>
        <label class="number-field"><span>封禁时长</span><div><input v-model.number="draft.duration_seconds" type="number" min="60" max="900" step="1" required :disabled="busy"><em>秒</em></div></label>
        <label class="number-field"><span>规则观察窗口</span><div><input v-model.number="draft.window_seconds" type="number" min="60" max="900" step="1" required :disabled="busy"><em>秒</em></div></label>
        <label class="number-field"><span>SSH 失败阈值</span><div><input v-model.number="draft.ssh_threshold" type="number" min="20" max="200" step="1" required :disabled="busy"><em>次</em></div></label>
        <label class="number-field"><span>Web 探测阈值</span><div><input v-model.number="draft.web_threshold" type="number" min="30" max="500" step="1" required :disabled="busy"><em>次</em></div></label>
        <div class="enable-field ai-field full-field">
          <label><input v-model="draft.ai_anomaly_enabled" type="checkbox" :disabled="loading || busy || !snapshot || Boolean(loadError) || unconfirmedSave || !verified || !draft.enabled"><span>小菱主动研判异常</span></label>
          <small>需同时启用自动封禁。仅研判可信日志候选，可提前临时封禁单个 IP，最多 2 分钟；每天最多 24 次模型研判。</small>
          <small>硬下限：SSH 认证失败至少 10 次，或 Web 探测至少 10 次且涉及 3 个敏感目标；执行器会重新读取日志核验证据与保护名单。不能任意封 IP、网段或永久封禁。</small>
        </div>
        <label class="allowlist-field full-field"><span>额外保护白名单</span><textarea v-model="allowlistText" rows="3" placeholder="每行一个 IP/CIDR，例如 198.51.100.8/32" :disabled="busy" /><small>最多 32 条；IPv4 网段前缀至少 /24，IPv6 至少 /64。系统保护来源始终优先。</small></label>
        <button class="blocking-button save-button full-field" type="submit" :disabled="saveDisabled">{{ saving ? '提交并核验中…' : '保存自动封禁策略' }}</button>
      </form>
      <aside class="blocking-rules">
        <h4>规则和保护范围</h4>
        <ul class="rule-notes">
          <li><strong>SSH：</strong>短窗口内重复出现 Failed password，达到阈值后才进入封禁评估。</li>
          <li><strong>Web：</strong>敏感路径探测达到阈值，且涉及多个不同目标才进入封禁评估；普通 403 不触发封禁。</li>
          <li><strong>作用范围：</strong>宿主机入站（INPUT）与 Prism Docker 的 80/443 流量。</li>
          <li><strong>小菱研判：</strong>使用可信日志候选，实际封禁前由执行器重新核验证据门槛与保护名单。</li>
          <li><strong>执行依据：</strong>命中或研判建议均不代表生效，以执行器回执显示结果。</li>
        </ul>
        <p class="family-support"><span>IPv4：{{ verified && snapshot?.family_support ? snapshot.family_support.ipv4 ? '已核验支持' : '不支持' : '待核验' }}</span><span>IPv6：{{ verified && snapshot?.family_support ? snapshot.family_support.ipv6 ? '已核验支持' : '不支持' : '待核验' }}</span></p>
        <h4>固定保护来源</h4>
        <p v-if="!snapshot || loadError" class="blocking-muted">保护名单尚未核验。</p>
        <p v-else-if="!snapshot.protected_sources.length" class="blocking-muted">当前回执未返回保护来源，请核验执行器配置。</p>
        <ul v-else class="protected-list"><li v-for="source in snapshot.protected_sources" :key="source.cidr"><code>{{ source.cidr }}</code><span>{{ source.reason }}</span><small>系统保护</small></li></ul>
      </aside>
    </div>

    <div class="block-record-heading"><h4>封禁与解封记录</h4><span>命中规则不等于已封禁，生效状态依据执行回执。</span></div>
    <p v-if="loading" class="records-empty" role="status">正在核验封禁记录…</p>
    <p v-else-if="loadError" class="records-empty">当前封禁记录未能读取，请刷新核验。</p>
    <p v-else-if="!records.length" class="records-empty">暂无匹配的封禁记录；这不等于没有探测或攻击。</p>
    <ul v-else class="block-records">
      <li v-for="entry in records" :key="entry.id" class="block-record">
        <div class="block-record-top"><code>{{ entry.ip }}</code><span class="record-state" :class="`record-${(verified && !reachedExpiry(entry)) || entry.status === 'failed' ? entry.status : 'unknown'}`">{{ recordStatus(entry) }}</span></div>
        <p><strong>{{ ruleLabel(entry) }}</strong> · 证据 {{ entry.evidence_count }} 次</p>
        <dl><div><dt>作用范围</dt><dd>{{ scopeLabel(entry.scope) }}</dd></div><div><dt>生效记录</dt><dd>{{ formatTime(entry.started_at) }}</dd></div><div><dt>到期时间</dt><dd>{{ formatTime(entry.expires_at) }}</dd></div><div v-if="entry.released_at"><dt>解封记录</dt><dd>{{ formatTime(entry.released_at) }}</dd></div></dl>
        <p v-if="entry.reason" class="record-reason">依据 / 处置：{{ entry.reason }}</p>
        <button v-if="entry.status === 'active'" class="blocking-button release-button" type="button" :disabled="busy || !verified" @click="release(entry)">{{ releasingIp === entry.ip ? '解封核验中…' : '手动解封' }}</button>
      </li>
    </ul>
  </section>
</template>

<style scoped lang="scss">
.blocking-panel { min-width: 0; padding: 22px; border: 1px solid #dedcff; border-radius: 17px; background: linear-gradient(120deg, #fff, #fafaff); color: var(--gray-800); box-shadow: 0 4px 16px rgba(26,35,66,.035); }
.blocking-heading, .blocking-title-group, .blocking-status-row, .block-record-top, .block-record-heading { display: flex; align-items: center; gap: 12px; }
.blocking-heading { align-items: flex-start; justify-content: space-between; gap: 20px; }
.blocking-title-group { min-width: 0; align-items: flex-start; }
.mini-shield { display: grid; width: 44px; height: 48px; flex: 0 0 44px; place-items: center; border-radius: 14px; background: #efeeff; }
.mini-shield svg { width: 29px; fill: rgba(111,103,242,.12); stroke: var(--brand-500); stroke-width: 2.3; stroke-linecap: round; stroke-linejoin: round; }
.eyebrow { color: var(--brand-500); font: 700 10px/1.4 var(--font-mono); letter-spacing: .16em; }
h3 { margin: 3px 0 5px; font-size: 19px; line-height: 1.4; }
.blocking-heading p { margin: 0; color: var(--gray-600); font-size: 12px; line-height: 1.65; }
.blocking-button { display: inline-flex; min-height: 40px; justify-content: center; align-items: center; gap: 6px; padding: 0 12px; border: 1px solid var(--gray-200); border-radius: 9px; color: var(--gray-700); background: white; font: 600 12px/1.4 var(--font-sans); cursor: pointer; }
.blocking-button svg { width: 15px; height: 15px; }
.blocking-button:disabled { opacity: .55; cursor: not-allowed; }
button:focus-visible, input:focus-visible, textarea:focus-visible { outline: 3px solid rgba(91,88,232,.28); outline-offset: 2px; }
.refresh-button { flex: 0 0 auto; }
.blocking-status-row { flex-wrap: wrap; gap: 8px 16px; margin: 18px 0; color: var(--gray-500); font-size: 11px; }
.blocking-state { display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px; border-radius: 999px; color: var(--gray-600); background: var(--gray-100); font-size: 12px; font-weight: 700; }
.blocking-state svg { width: 14px; height: 14px; }
.blocking-state.enabled { color: #187b73; background: #e9f8f4; }
.blocking-state.warning { color: #a96f24; background: #fff2d9; }
.blocking-error, .blocking-warning, .executor-errors { margin: 10px 0; padding: 10px 13px; border: 1px solid #f2c8d0; border-radius: 10px; color: #a4374b; background: #fff4f5; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.blocking-warning, .executor-errors { color: #8d6225; border-color: #f1dfb9; background: #fff9eb; }
.executor-errors { padding-left: 28px; }
.blocking-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; }
.blocking-form { display: grid; min-width: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; align-content: start; }
.full-field { grid-column: 1 / -1; }
.enable-field label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; cursor: pointer; }
.enable-field input { width: 18px; height: 18px; margin: 0; accent-color: var(--brand-500); }
.enable-field small, .allowlist-field small { display: block; margin-top: 5px; color: var(--gray-500); font-size: 10px; line-height: 1.65; }
.ai-field { padding: 11px 12px; border: 1px solid #e1dcfb; border-radius: 10px; background: #f8f6ff; }
.family-support { display: flex; flex-wrap: wrap; gap: 7px 16px; margin: 0 0 15px; color: var(--gray-500); font-size: 10px; line-height: 1.6; }
.number-field, .allowlist-field { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.number-field > span, .allowlist-field > span { font-size: 11px; font-weight: 600; }
.number-field > div { display: flex; align-items: center; min-width: 0; border: 1px solid var(--gray-200); border-radius: 8px; background: white; }
.number-field input { width: 100%; min-width: 0; min-height: 38px; padding: 0 10px; border: 0; border-radius: 8px; color: var(--gray-800); background: transparent; font: 12px var(--font-mono); }
.number-field em { flex: 0 0 auto; padding-right: 9px; color: var(--gray-500); font-size: 10px; font-style: normal; }
.allowlist-field textarea { box-sizing: border-box; width: 100%; min-width: 0; min-height: 78px; padding: 8px 10px; border: 1px solid var(--gray-200); border-radius: 8px; color: var(--gray-700); background: white; font: 11px/1.6 var(--font-mono); resize: vertical; overflow-wrap: anywhere; }
.save-button { color: white; border-color: transparent; background: linear-gradient(135deg, var(--brand-500), #766cf0); }
.blocking-rules { min-width: 0; padding: 15px; border: 1px solid var(--gray-100); border-radius: 12px; background: var(--gray-50); }
h4 { margin: 0; color: var(--gray-800); font-size: 13px; line-height: 1.5; }
.rule-notes { display: flex; flex-direction: column; gap: 7px; margin: 10px 0 17px; padding-left: 17px; color: var(--gray-600); font-size: 11px; line-height: 1.65; }
.rule-notes strong { color: var(--gray-700); }
.protected-list { display: flex; flex-direction: column; gap: 7px; margin: 9px 0 0; padding: 0; list-style: none; }
.protected-list li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 8px; padding: 7px 9px; border: 1px solid var(--gray-200); border-radius: 8px; background: white; }
.protected-list code { grid-column: 1; overflow-wrap: anywhere; color: var(--gray-800); font: 11px/1.5 var(--font-mono); }
.protected-list span { grid-column: 1; color: var(--gray-500); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.protected-list small { grid-column: 2; grid-row: 1 / span 2; align-self: center; color: #218d7c; font-size: 9px; }
.blocking-muted { color: var(--gray-500); font-size: 11px; line-height: 1.6; }
.block-record-heading { flex-wrap: wrap; justify-content: space-between; gap: 6px 12px; margin-top: 22px; padding-top: 17px; border-top: 1px solid var(--gray-200); }
.block-record-heading > span { color: var(--gray-500); font-size: 10px; line-height: 1.6; }
.records-empty { margin: 13px 0 0; color: var(--gray-500); font-size: 12px; line-height: 1.6; }
.block-records { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 13px 0 0; padding: 0; list-style: none; }
.block-record { min-width: 0; padding: 13px; border: 1px solid var(--gray-200); border-radius: 12px; background: #fff; }
.block-record-top { align-items: flex-start; justify-content: space-between; gap: 8px; }
.block-record-top code { min-width: 0; overflow-wrap: anywhere; color: var(--gray-900); font: 700 12px/1.7 var(--font-mono); }
.record-state { flex: 0 0 auto; padding: 3px 7px; border-radius: 99px; color: var(--gray-600); background: var(--gray-100); font-size: 10px; line-height: 1.6; }
.record-active { color: #187b73; background: #e9f8f4; }
.record-failed { color: #a4374b; background: #fff0f3; }
.record-unknown { color: #a96f24; background: #fff5df; }
.block-record p { margin: 8px 0; color: var(--gray-600); font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.block-record dl { display: flex; flex-direction: column; gap: 5px; margin: 10px 0; }
.block-record dl div { display: grid; grid-template-columns: 60px minmax(0, 1fr); gap: 7px; font-size: 10px; line-height: 1.6; }
.block-record dt { color: var(--gray-500); }
.block-record dd { margin: 0; color: var(--gray-700); overflow-wrap: anywhere; }
.release-button { min-height: 36px; margin-top: 3px; color: var(--brand-600); border-color: #dcd8fb; background: #faf9ff; }
@media (max-width: 920px) { .blocking-layout { grid-template-columns: 1fr; gap: 16px; } }
@media (max-width: 640px) {
  .blocking-panel { padding: 14px; border-radius: 14px; }
  .blocking-heading { flex-direction: column; gap: 12px; }
  .blocking-title-group { gap: 10px; }
  .mini-shield { width: 36px; height: 41px; flex-basis: 36px; }
  .mini-shield svg { width: 24px; }
  h3 { font-size: 17px; }
  .blocking-heading p { font-size: 11px; }
  .refresh-button { align-self: stretch; }
  .blocking-status-row { align-items: flex-start; flex-direction: column; gap: 8px; margin: 14px 0; }
  .blocking-form { gap: 11px; }
  .blocking-rules { padding: 12px; }
  .block-records { grid-template-columns: 1fr; }
  .blocking-button { min-height: 42px; }
  .release-button { width: 100%; }
}
@media (max-width: 360px) { .blocking-form { grid-template-columns: 1fr; } }
</style>
