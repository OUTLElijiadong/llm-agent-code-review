<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
import { mustDiscardReadSnapshot, readableError } from '@/composables/withFeedback'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  createTicket, getTickets, handleTicket, closeTicket, type Ticket,
} from '@/api/maintenance'

const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin())

const CATEGORY: Record<string, string> = {
  bug: '功能报错', account: '账号异常', feature: '功能建议',
  performance: '性能问题', other: '其他',
}
const PRIORITY: Record<string, string> = { low: '低', medium: '中', high: '高' }
const STATUS: Record<string, string> = {
  pending: '待处理', processing: '处理中', resolved: '已解决', closed: '已关闭',
}
const STATUS_TAG: Record<string, string> = {
  pending: 'info', processing: 'warning', resolved: 'success', closed: '',
}

const tickets = ref<Ticket[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const statusFilter = ref('')
const scope = ref<'mine' | 'all'>('mine')
const loadError = ref('')
const actionError = ref('')
let loadGeneration = 0
const loading = ref(false)
const submitting = ref(false)

const submitVisible = ref(false)
const form = reactive({ title: '', description: '', category: 'bug', priority: 'medium' })

const handleVisible = ref(false)
const current = ref<Ticket | null>(null)
const handleForm = reactive({ status: '', admin_reply: '', priority: '' })

async function load() {
  const generation = ++loadGeneration
  loading.value = true
  loadError.value = ''
  try {
    const res = await getTickets({
      page: page.value, page_size: pageSize.value,
      status: statusFilter.value, scope: isAdmin.value ? scope.value : 'mine',
    })
    if (generation !== loadGeneration) return
    tickets.value = res.items
    total.value = res.total
  } catch (error) {
    if (generation !== loadGeneration) return
    if (mustDiscardReadSnapshot(error)) { tickets.value = []; total.value = 0 }
    loadError.value = readableError(error, '工单读取失败，请重试')
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function submit() {
  if (!form.title.trim() || !form.description.trim()) {
    ElMessage.warning('请填写标题和问题描述')
    return
  }
  if (submitting.value) return
  submitting.value = true
  actionError.value = ''
  try {
    await createTicket({ ...form })
    ElMessage.success('工单已提交')
    submitVisible.value = false
    Object.assign(form, { title: '', description: '', category: 'bug', priority: 'medium' })
    page.value = 1
    load()
  } catch (error) {
    actionError.value = readableError(error, '提交失败，内容已保留，请核对列表后再试')
  } finally {
    submitting.value = false
  }
}

function openHandle(t: Ticket) {
  current.value = t
  Object.assign(handleForm, { status: t.status, admin_reply: t.admin_reply || '', priority: t.priority })
  handleVisible.value = true
}

const actionBusy = ref(false)
async function submitHandle() {
  if (!current.value || actionBusy.value) return
  actionBusy.value = true
  actionError.value = ''
  try {
    await handleTicket(current.value.id, { ...handleForm })
    ElMessage.success('已更新工单')
    handleVisible.value = false
    await load()
  } catch (error) { actionError.value = readableError(error, '更新失败，内容已保留') }
  finally { actionBusy.value = false }
}

async function close(t: Ticket) {
  if (actionBusy.value) return
  actionBusy.value = true
  actionError.value = ''
  try {
    await ElMessageBox.confirm('确认关闭该工单?', '提示', { type: 'warning' })
    await closeTicket(t.id)
    ElMessage.success('已关闭')
    await load()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') actionError.value = readableError(error, '关闭失败，请重新核对工单状态')
  } finally { actionBusy.value = false }
}

onMounted(load)
</script>

<template>
  <div class="maintenance-page">
    <div class="page-header">
      <div>
        <h2>申请维修</h2>
        <p class="page-sub">遇到平台故障、账号异常或功能报错,提交工单由管理员受理处理</p>
      </div>
      <el-button type="primary" @click="submitVisible = true">提交工单</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-radio-group v-if="isAdmin" v-model="scope" @change="() => { page = 1; load() }">
          <el-radio-button value="mine">我的工单</el-radio-button>
          <el-radio-button value="all">全部工单</el-radio-button>
        </el-radio-group>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 160px"
          @change="() => { page = 1; load() }">
          <el-option v-for="(label, val) in STATUS" :key="val" :label="label" :value="val" />
        </el-select>
      </div>
    </el-card>

    <el-alert v-if="loadError" type="error" :closable="false" :title="loadError"><el-button :loading="loading" @click="load">重新加载</el-button></el-alert>
    <el-alert v-if="actionError && !submitVisible" type="error" :closable="false" :title="actionError" />
    <el-card shadow="never">
      <div v-loading="loading" class="support-records">
        <article v-for="row in tickets" :key="row.id" class="support-record">
          <header><strong>{{ row.title }}</strong><el-tag size="small" :type="STATUS_TAG[row.status] as any">{{ STATUS[row.status] }}</el-tag></header>
          <p>{{ CATEGORY[row.category] || row.category }} · {{ PRIORITY[row.priority] }}优先级 · #{{ row.id }}</p>
          <details><summary>查看问题与回复</summary><p>{{ row.description }}</p><p v-if="row.admin_reply">管理员回复：{{ row.admin_reply }}</p></details>
          <footer><time>{{ formatDate(row.create_time) }}</time><div><el-button v-if="isAdmin" link type="primary" :disabled="loading || actionBusy" @click="openHandle(row)">受理</el-button><el-button v-if="row.status !== 'closed'" link type="info" :disabled="loading || actionBusy" @click="close(row)">关闭工单</el-button></div></footer>
        </article>
        <el-empty v-if="!loading && !loadError && !tickets.length" description="暂无工单，需要帮助时可提交问题" />
      </div>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; load() }" />
      </div>
    </el-card>

    <!-- 提交工单 -->
    <el-dialog v-model="submitVisible" title="提交维修工单" width="560px">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <el-form label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="150" placeholder="一句话描述问题" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width: 100%">
            <el-option v-for="(label, val) in CATEGORY" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="form.priority" style="width: 100%">
            <el-option v-for="(label, val) in PRIORITY" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="问题描述" required>
          <el-input v-model="form.description" type="textarea" :rows="5"
            placeholder="复现步骤、报错信息、期望结果等" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="submitVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">提交</el-button>
      </template>
    </el-dialog>

    <!-- 管理员受理 -->
    <el-dialog v-model="handleVisible" title="受理工单" width="560px">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <template v-if="current">
        <el-descriptions :column="1" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="标题">{{ current.title }}</el-descriptions-item>
          <el-descriptions-item label="描述">
            <span style="white-space: pre-wrap">{{ current.description }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-form label-width="80px">
          <el-form-item label="状态">
            <el-select v-model="handleForm.status" style="width: 100%">
              <el-option v-for="(label, val) in STATUS" :key="val" :label="label" :value="val" />
            </el-select>
          </el-form-item>
          <el-form-item label="处理回复">
            <el-input v-model="handleForm.admin_reply" type="textarea" :rows="4" />
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="handleVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionBusy" @click="submitHandle">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.support-records { display: grid; gap: 14px; }
.support-record { padding: 18px; border: 1px solid var(--el-border-color-light); border-radius: 14px; min-width: 0; }
.support-record header, .support-record footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.support-record header strong { min-width: 0; overflow-wrap: anywhere; }
.support-record p { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.support-record summary { cursor: pointer; color: var(--el-color-primary); }
.support-record footer { margin-top: 14px; font-size: 12px; color: var(--el-text-color-secondary); }
.record-preview { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.page-header { flex-wrap: wrap; gap: 14px; }

.maintenance-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.filter-card { margin-bottom: 12px; }
.filter-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
