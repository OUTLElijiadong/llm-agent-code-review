<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'

import { useUserStore } from '@/stores/user'
import { formatDateTime, parseUtcTimestamp } from '@/utils/format'
import { mustDiscardReadSnapshot, readableError } from '@/composables/withFeedback'
import { createFeedback, getFeedbackList, markFeedbackRead, replyFeedback, type Feedback } from '@/api/feedback'

const props = withDefaults(defineProps<{ active?: boolean }>(), { active: true })
const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin())
const accountKey = computed(() => [userStore.profile?.id, userStore.profile?.role, isAdmin.value].join(':'))
const TYPE: Record<string, string> = {
  suggestion: '建议', complaint: '投诉', praise: '表扬', bug: '问题', other: '其他',
}
const STATUS: Record<string, string> = { new: '待查看', read: '已读', replied: '已回复', closed: '已关闭' }
const STATUS_TAG: Record<string, 'danger' | 'info' | 'success' | undefined> = {
  new: 'danger', read: 'info', replied: 'success', closed: undefined,
}
const SAVED_MESSAGE: Record<string, string> = {
  new: '反馈已设为待查看', read: '反馈已标为已读', replied: '反馈已回复', closed: '反馈已关闭',
}
const list = ref<Feedback[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const scope = ref<'mine' | 'all'>(isAdmin.value ? 'all' : 'mine')
const statusFilter = ref('')
const typeFilter = ref('')
const loadError = ref('')
const actionError = ref('')
const loading = ref(false)
const submitting = ref(false)
const replying = ref(false)
const readingId = ref<number | null>(null)
const progressNotice = ref(false)
const staleSnapshot = ref(false)
const authorizationBlocked = ref(false)
const busy = computed(() => submitting.value || replying.value || readingId.value !== null)

const submitVisible = ref(false)
const form = reactive({ feedback_type: 'suggestion', content: '', contact: '' })
const replyVisible = ref(false)
const current = ref<Feedback | null>(null)
const replyForm = reactive({ admin_reply: '', status: 'new' })
const replyStatusChosen = ref(false)
const replyError = computed(() => replyForm.status === 'replied' && !replyForm.admin_reply.trim()
  ? '标记为已回复时，请填写回复内容' : '')
const replyDirty = computed(() => current.value !== null && (
  replyForm.admin_reply.trim() !== (current.value.admin_reply || '').trim()
  || replyForm.status !== current.value.status
))
const replyDisabled = computed(() => busy.value || !replyDirty.value || !!replyError.value || !STATUS[replyForm.status])

let mounted = false
let disposed = false
let loadGeneration = 0
let accountGeneration = 0
let refreshTimer: ReturnType<typeof setTimeout> | undefined

function stopRefresh() {
  if (refreshTimer !== undefined) clearTimeout(refreshTimer)
  refreshTimer = undefined
}
function canRefresh() {
  return mounted && !disposed && props.active && !document.hidden && !loading.value
    && !busy.value && !submitVisible.value && !replyVisible.value && !authorizationBlocked.value
}
function scheduleRefresh() {
  stopRefresh()
  if (canRefresh()) refreshTimer = setTimeout(() => { refreshTimer = undefined; void load(true) }, 30_000)
}
function handlingChanged(before: Feedback, after: Feedback) {
  return before.status !== after.status || before.admin_reply !== after.admin_reply || before.handled_at !== after.handled_at
}
async function load(automatic = false) {
  if (disposed || (automatic && !canRefresh())) return
  stopRefresh()
  const generation = ++loadGeneration
  const requestedAccount = accountKey.value
  loading.value = true
  loadError.value = ''
  try {
    const res = await getFeedbackList({ page: page.value, page_size: pageSize,
      scope: isAdmin.value ? scope.value : 'mine', status: statusFilter.value, feedback_type: typeFilter.value })
    if (disposed || generation !== loadGeneration || requestedAccount !== accountKey.value) return
    const lastPage = Math.max(1, Math.ceil(res.total / pageSize))
    if (page.value > lastPage) { page.value = lastPage; await load(); return }
    const previous = new Map(list.value.map(row => [row.id, row]))
    if (res.items.some(row => row.user_id === userStore.profile?.id && previous.has(row.id)
      && handlingChanged(previous.get(row.id)!, row))) progressNotice.value = true
    list.value = res.items
    total.value = res.total
    staleSnapshot.value = false
    authorizationBlocked.value = false
  } catch (error) {
    if (disposed || generation !== loadGeneration || requestedAccount !== accountKey.value) return
    if (mustDiscardReadSnapshot(error)) {
      list.value = []; total.value = 0; progressNotice.value = false
      authorizationBlocked.value = true
    }
    staleSnapshot.value = list.value.length > 0
    loadError.value = readableError(error, '反馈读取失败，请重试')
  } finally {
    if (!disposed && generation === loadGeneration) { loading.value = false; scheduleRefresh() }
  }
}
function refresh() { void load() }
function changeFilters() { page.value = 1; progressNotice.value = false; refresh() }
function changePage(value: number) { page.value = value; refresh() }
function displayTime(value: string | null | undefined) {
  const timestamp = parseUtcTimestamp(value)
  return Number.isFinite(timestamp) ? formatDateTime(timestamp) : '—'
}
function openSubmit() { actionError.value = ''; submitVisible.value = true }

async function submit() {
  if (!form.content.trim()) { ElMessage.warning('请填写反馈内容'); return }
  if (busy.value) return
  const generation = accountGeneration
  submitting.value = true
  actionError.value = ''
  stopRefresh()
  try {
    await createFeedback({ ...form, content: form.content.trim(), contact: form.contact.trim() })
    if (disposed || generation !== accountGeneration) return
    ElMessage.success('反馈已提交，感谢你的建议')
    submitVisible.value = false
    Object.assign(form, { feedback_type: 'suggestion', content: '', contact: '' })
    page.value = 1
    await load()
  } catch (error) {
    if (!disposed && generation === accountGeneration) actionError.value = readableError(error, '提交失败，内容已保留，请核对列表后再试')
  } finally {
    if (!disposed && generation === accountGeneration) { submitting.value = false; scheduleRefresh() }
  }
}
function openReply(row: Feedback) {
  actionError.value = ''
  current.value = row
  Object.assign(replyForm, { admin_reply: row.admin_reply || '', status: row.status })
  replyStatusChosen.value = false
  replyVisible.value = true
}
async function submitReply() {
  if (!isAdmin.value || !current.value || replyDisabled.value) return
  const generation = accountGeneration
  const previousReply = current.value.admin_reply || ''
  replying.value = true
  actionError.value = ''
  stopRefresh()
  try {
    const updated = await replyFeedback(current.value.id, { admin_reply: replyForm.admin_reply.trim(), status: replyForm.status })
    if (disposed || generation !== accountGeneration) return
    const message = SAVED_MESSAGE[updated.status] || '反馈已更新'
    const replySaved = !!updated.admin_reply && updated.admin_reply !== previousReply
    ElMessage.success(replySaved && updated.status !== 'replied' ? '回复已保存，' + message : message)
    replyVisible.value = false
    current.value = null
    await load()
  } catch (error) {
    if (!disposed && generation === accountGeneration) actionError.value = readableError(error, '处理失败，内容已保留，请核对当前状态后再试')
  } finally {
    if (!disposed && generation === accountGeneration) { replying.value = false; scheduleRefresh() }
  }
}
async function markRead(row: Feedback) {
  if (!isAdmin.value || row.status !== 'new' || busy.value) return
  const generation = accountGeneration
  readingId.value = row.id
  actionError.value = ''
  stopRefresh()
  try {
    const updated = await markFeedbackRead(row.id)
    if (disposed || generation !== accountGeneration) return
    ElMessage.success(SAVED_MESSAGE[updated.status] || '反馈状态已更新')
    await load()
  } catch (error) {
    if (!disposed && generation === accountGeneration) actionError.value = readableError(error, '标记失败，请核对当前状态后再试')
  } finally {
    if (!disposed && generation === accountGeneration) { readingId.value = null; scheduleRefresh() }
  }
}
function visibilityChanged() {
  stopRefresh()
  if (canRefresh()) refresh()
}
watch(() => props.active, (active) => {
  stopRefresh()
  if (active && canRefresh()) refresh()
})
watch([submitVisible, replyVisible, busy], scheduleRefresh)
watch(() => replyForm.admin_reply, value => {
  if (!replyVisible.value || !current.value || replyStatusChosen.value || !['new', 'read'].includes(current.value.status)) return
  const contentChanged = value.trim() !== (current.value.admin_reply || '').trim()
  replyForm.status = contentChanged && value.trim() ? 'replied' : current.value.status
})
watch(accountKey, () => {
  ++accountGeneration; ++loadGeneration
  stopRefresh()
  list.value = []; total.value = 0; page.value = 1
  scope.value = isAdmin.value ? 'all' : 'mine'
  statusFilter.value = ''; typeFilter.value = ''
  loadError.value = ''; actionError.value = ''; progressNotice.value = false; staleSnapshot.value = false
  authorizationBlocked.value = false; loading.value = false
  submitting.value = false; replying.value = false; readingId.value = null
  submitVisible.value = false; replyVisible.value = false; current.value = null
  Object.assign(form, { feedback_type: 'suggestion', content: '', contact: '' })
  Object.assign(replyForm, { admin_reply: '', status: 'new' })
  if (canRefresh()) refresh()
})
onMounted(() => {
  mounted = true
  document.addEventListener('visibilitychange', visibilityChanged)
  if (canRefresh()) refresh()
})
onBeforeUnmount(() => {
  disposed = true; ++loadGeneration; ++accountGeneration
  stopRefresh()
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <div class="feedback-page">
    <div class="page-header">
      <div><h2>{{ isAdmin ? '反馈管理' : '向管理员反馈' }}</h2>
        <p class="page-sub">{{ isAdmin ? '查看、回复并跟进反馈处理进度。' : '提交建议或问题，在这里查看管理员的回复与处理进度。' }}</p>
      </div>
      <div class="header-actions">
        <el-button :loading="loading" :disabled="busy" @click="refresh">刷新反馈</el-button>
        <el-button type="primary" :disabled="busy" @click="openSubmit">提交反馈</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filters">
        <el-radio-group v-if="isAdmin" v-model="scope" aria-label="反馈范围" :disabled="loading || busy" @change="changeFilters">
          <el-radio-button value="all">全部反馈</el-radio-button><el-radio-button value="mine">我的反馈</el-radio-button>
        </el-radio-group>
        <label class="filter-field"><span>状态</span>
          <el-select v-model="statusFilter" aria-label="反馈状态" :disabled="loading || busy" @change="changeFilters">
            <el-option label="全部状态" value="" /><el-option v-for="(label, value) in STATUS" :key="value" :label="label" :value="value" />
          </el-select>
        </label>
        <label class="filter-field"><span>类型</span>
          <el-select v-model="typeFilter" aria-label="反馈类型" :disabled="loading || busy" @change="changeFilters">
            <el-option label="全部类型" value="" /><el-option v-for="(label, value) in TYPE" :key="value" :label="label" :value="value" />
          </el-select>
        </label>
      </div>
      <p class="refresh-note">此页显示时每 30 秒更新一次，编辑期间暂停；也可手动刷新。</p>
    </el-card>

    <el-alert v-if="progressNotice" title="反馈处理进度有更新，请展开详情查看。" type="success" @close="progressNotice = false" />
    <el-alert v-if="loadError" type="error" :closable="false" :title="loadError"><el-button :loading="loading" :disabled="busy" @click="refresh">重新加载</el-button></el-alert>
    <el-alert v-if="actionError && !submitVisible && !replyVisible" type="error" :closable="false" :title="actionError" />
    <p v-if="staleSnapshot" class="snapshot-note" role="status">当前展示上次读取结果，刷新成功后更新。</p>
    <el-card shadow="never">
      <div v-loading="loading" class="support-records">
        <article v-for="row in list" :key="row.id" class="support-record">
          <header><strong>{{ TYPE[row.feedback_type] || row.feedback_type }} · #{{ row.id }}</strong><el-tag :type="STATUS_TAG[row.status]">{{ STATUS[row.status] || '待核对' }}</el-tag></header>
          <p class="record-preview">{{ row.content }}</p>
          <details><summary>展开反馈详情</summary>
            <div class="record-detail"><p>{{ row.content }}</p>
              <p v-if="row.contact">联系方式：{{ row.contact }}</p>
              <div class="admin-reply"><strong>管理员回复</strong><p>{{ row.admin_reply || '暂无回复' }}</p></div>
              <dl class="record-meta">
                <div><dt>提交者</dt><dd>{{ row.user_id === userStore.profile?.id ? '我' : `用户 #${row.user_id}` }}</dd></div>
                <div><dt>提交时间</dt><dd><time>{{ displayTime(row.create_time) }}</time></dd></div>
                <div><dt>处理者</dt><dd>{{ row.handled_by ? `管理员 #${row.handled_by}` : '尚未处理' }}</dd></div>
                <div><dt>处理时间</dt><dd><time>{{ displayTime(row.handled_at) }}</time></dd></div>
              </dl>
            </div>
          </details>
          <footer><time>{{ displayTime(row.create_time) }}</time>
            <div v-if="isAdmin" class="record-actions">
              <el-button v-if="row.status === 'new'" :loading="readingId === row.id" :disabled="loading || busy" @click="markRead(row)">标为已读</el-button>
              <el-button type="primary" plain :disabled="loading || busy" @click="openReply(row)">回复 / 处理</el-button>
            </div>
          </footer>
        </article>
        <el-empty v-if="!loading && !loadError && !list.length" :description="statusFilter || typeFilter ? '当前筛选下暂无反馈记录' : '暂无反馈记录，可提交你的建议'" />
      </div>
      <div class="pager"><el-pagination layout="total, prev, pager, next" :pager-count="5" :total="total" :page-size="pageSize"
        :current-page="page" :disabled="loading || busy" @current-change="changePage" /></div>
    </el-card>

    <el-dialog v-model="submitVisible" title="提交反馈" width="540px" :close-on-click-modal="false" :close-on-press-escape="!submitting" :show-close="!submitting">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <el-form label-position="top">
        <el-form-item label="类型"><el-select v-model="form.feedback_type" aria-label="提交反馈类型" style="width: 100%" :disabled="submitting">
          <el-option v-for="(label, value) in TYPE" :key="value" :label="label" :value="value" /></el-select></el-form-item>
        <el-form-item label="内容" required><el-input v-model="form.content" aria-label="反馈内容" type="textarea" :rows="5" maxlength="20000" show-word-limit :disabled="submitting" placeholder="请描述你的建议或问题" /></el-form-item>
        <el-form-item label="联系方式"><el-input v-model="form.contact" aria-label="反馈联系方式" placeholder="选填，便于回访" maxlength="100" :disabled="submitting" /></el-form-item>
      </el-form>
      <template #footer><el-button :disabled="submitting" @click="submitVisible = false">取消</el-button><el-button type="primary" :loading="submitting" :disabled="!form.content.trim()" @click="submit">提交</el-button></template>
    </el-dialog>

    <el-dialog v-model="replyVisible" title="回复与处理反馈" width="540px" :close-on-click-modal="false" :close-on-press-escape="!replying" :show-close="!replying">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <template v-if="current">
        <el-descriptions :column="1" border size="small" class="reply-context">
          <el-descriptions-item label="当前状态">{{ STATUS[current.status] || '待核对' }}</el-descriptions-item>
          <el-descriptions-item label="提交者">{{ current.user_id === userStore.profile?.id ? '我' : `用户 #${current.user_id}` }}</el-descriptions-item>
          <el-descriptions-item label="内容"><span class="feedback-content">{{ current.content }}</span></el-descriptions-item>
          <el-descriptions-item v-if="current.contact" label="联系方式">{{ current.contact }}</el-descriptions-item>
        </el-descriptions>
        <el-form label-position="top">
          <el-form-item label="回复"><el-input v-model="replyForm.admin_reply" aria-label="管理员回复" type="textarea" :rows="4" maxlength="20000" show-word-limit :disabled="replying" /></el-form-item>
          <el-form-item label="处理状态"><el-select v-model="replyForm.status" aria-label="反馈处理状态" style="width: 100%" :disabled="replying" @change="replyStatusChosen = true">
            <el-option v-for="(label, value) in STATUS" :key="value" :label="label" :value="value" /></el-select></el-form-item>
        </el-form>
        <p v-if="replyError" class="validation-note" role="alert">{{ replyError }}</p>
        <p class="refresh-note">保存后提交者可在本人的反馈中查看最新回复。修改原回复会保留处理审计。</p>
      </template>
      <template #footer><el-button :disabled="replying" @click="replyVisible = false">取消</el-button><el-button type="primary" :loading="replying" :disabled="replyDisabled" @click="submitReply">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.feedback-page { padding: 4px; min-width: 0; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 14px; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 14px; line-height: 1.6; }
.header-actions, .filters, .record-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.header-actions :deep(.el-button), .record-actions :deep(.el-button) { margin-left: 0; }
.filter-card { margin-bottom: 12px; }
.filters { align-items: flex-end; gap: 12px; }
.filter-field { display: grid; gap: 6px; flex: 0 1 160px; min-width: 120px; font-size: 13px; }
.refresh-note, .snapshot-note { font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.refresh-note { margin: 12px 0 0; }
.snapshot-note { color: var(--el-color-warning-dark-2); }
.validation-note { color: var(--el-color-danger); font-size: 14px; }
.feedback-page > :deep(.el-alert) { margin-bottom: 12px; }
.support-records { display: grid; gap: 14px; }
.support-record { padding: 18px; border: 1px solid var(--el-border-color-light); border-radius: 14px; min-width: 0; }
.support-record header, .support-record footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.support-record header strong { min-width: 0; overflow-wrap: anywhere; }
.support-record p, .feedback-content { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; font-size: 14px; }
.support-record summary { cursor: pointer; color: var(--el-color-primary); min-height: 44px; display: list-item; align-content: center; font-size: 14px; }
.support-record summary:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; border-radius: 4px; }
.support-record footer { margin-top: 14px; font-size: 12px; color: var(--el-text-color-secondary); }
.record-preview { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.admin-reply { padding: 12px; background: var(--el-fill-color-light); border-radius: 8px; }
.admin-reply p { margin-bottom: 0; }
.record-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; font-size: 12px; }
.record-meta dt { color: var(--el-text-color-secondary); }
.record-meta dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.reply-context { margin-bottom: 16px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; max-width: 100%; }
.feedback-page :deep(.el-button), .feedback-page :deep(.el-select__wrapper) { min-height: 44px; }
@media (max-width: 520px) {
  .support-record { padding: 14px; }
  .record-meta { grid-template-columns: 1fr; }
  .record-actions { width: 100%; }
  .filters > :deep(.el-radio-group) { width: 100%; }
  .filter-field { flex: 1 1 120px; }
  .pager { justify-content: flex-start; overflow-x: auto; }
}
</style>
