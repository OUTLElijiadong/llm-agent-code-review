<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
import { mustDiscardReadSnapshot, readableError } from '@/composables/withFeedback'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  createFeedback, getFeedbackList, replyFeedback, type Feedback,
} from '@/api/feedback'

const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin())

const TYPE: Record<string, string> = {
  suggestion: '建议', complaint: '投诉', praise: '表扬', bug: '问题', other: '其他',
}
const STATUS: Record<string, string> = {
  new: '待查看', read: '已读', replied: '已回复', closed: '已关闭',
}
const STATUS_TAG: Record<string, string> = {
  new: 'danger', read: 'info', replied: 'success', closed: '',
}

const list = ref<Feedback[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const scope = ref<'mine' | 'all'>('mine')
const loadError = ref('')
const actionError = ref('')
let loadGeneration = 0
const loading = ref(false)
const submitting = ref(false)

const submitVisible = ref(false)
const form = reactive({ feedback_type: 'suggestion', content: '', contact: '' })

const replyVisible = ref(false)
const current = ref<Feedback | null>(null)
const replyForm = reactive({ admin_reply: '', status: 'replied' })

async function load() {
  const generation = ++loadGeneration
  loading.value = true
  loadError.value = ''
  try {
    const res = await getFeedbackList({
      page: page.value, page_size: pageSize.value,
      scope: isAdmin.value ? scope.value : 'mine',
    })
    if (generation !== loadGeneration) return
    list.value = res.items
    total.value = res.total
  } catch (error) {
    if (generation !== loadGeneration) return
    if (mustDiscardReadSnapshot(error)) { list.value = []; total.value = 0 }
    loadError.value = readableError(error, '反馈读取失败，请重试')
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function submit() {
  if (!form.content.trim()) {
    ElMessage.warning('请填写反馈内容')
    return
  }
  if (submitting.value) return
  submitting.value = true
  actionError.value = ''
  try {
    await createFeedback({ ...form })
    ElMessage.success('反馈已提交,感谢你的建议')
    submitVisible.value = false
    Object.assign(form, { feedback_type: 'suggestion', content: '', contact: '' })
    page.value = 1
    load()
  } catch (error) {
    actionError.value = readableError(error, '提交失败，内容已保留，请核对列表后再试')
  } finally {
    submitting.value = false
  }
}

function openReply(f: Feedback) {
  current.value = f
  Object.assign(replyForm, { admin_reply: f.admin_reply || '', status: 'replied' })
  replyVisible.value = true
}

const replying = ref(false)
async function submitReply() {
  if (!current.value || replying.value) return
  replying.value = true
  actionError.value = ''
  try {
    await replyFeedback(current.value.id, { ...replyForm })
    ElMessage.success('已回复')
    replyVisible.value = false
    await load()
  } catch (error) { actionError.value = readableError(error, '回复失败，内容已保留') }
  finally { replying.value = false }
}

onMounted(load)
</script>

<template>
  <div class="feedback-page">
    <div class="page-header">
      <div>
        <h2>向管理员反馈</h2>
        <p class="page-sub">产品建议、体验问题、投诉或表扬,都会直达管理员</p>
      </div>
      <el-button type="primary" @click="submitVisible = true">提交反馈</el-button>
    </div>

    <el-card v-if="isAdmin" shadow="never" class="filter-card">
      <el-radio-group v-model="scope" @change="() => { page = 1; load() }">
        <el-radio-button value="mine">我的反馈</el-radio-button>
        <el-radio-button value="all">全部反馈</el-radio-button>
      </el-radio-group>
    </el-card>

    <el-alert v-if="loadError" type="error" :closable="false" :title="loadError"><el-button :loading="loading" @click="load">重新加载</el-button></el-alert>
    <el-alert v-if="actionError && !submitVisible" type="error" :closable="false" :title="actionError" />
    <el-card shadow="never">
      <div v-loading="loading" class="support-records">
        <article v-for="row in list" :key="row.id" class="support-record">
          <header><strong>{{ TYPE[row.feedback_type] || row.feedback_type }} · #{{ row.id }}</strong><el-tag size="small" :type="STATUS_TAG[row.status] as any">{{ STATUS[row.status] }}</el-tag></header>
          <p class="record-preview">{{ row.content }}</p>
          <details><summary>展开反馈详情</summary><p>{{ row.content }}</p><p v-if="row.admin_reply">管理员回复：{{ row.admin_reply }}</p></details>
          <footer><time>{{ formatDate(row.create_time) }}</time><el-button v-if="isAdmin" link type="primary" :disabled="loading" @click="openReply(row)">回复</el-button></footer>
        </article>
        <el-empty v-if="!loading && !loadError && !list.length" description="暂无反馈记录，可提交你的建议" />
      </div>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; load() }" />
      </div>
    </el-card>

    <el-dialog v-model="submitVisible" title="提交反馈" width="540px">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <el-form label-width="80px">
        <el-form-item label="类型">
          <el-select v-model="form.feedback_type" style="width: 100%">
            <el-option v-for="(label, val) in TYPE" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="form.content" type="textarea" :rows="5" placeholder="请描述你的建议或问题" />
        </el-form-item>
        <el-form-item label="联系方式">
          <el-input v-model="form.contact" placeholder="选填,便于回访" maxlength="100" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="submitVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">提交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="replyVisible" title="回复反馈" width="540px">
      <el-alert v-if="actionError" type="error" :closable="false" :title="actionError" />
      <template v-if="current">
        <el-descriptions :column="1" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="内容">
            <span style="white-space: pre-wrap">{{ current.content }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="current.contact" label="联系方式">{{ current.contact }}</el-descriptions-item>
        </el-descriptions>
        <el-form label-width="80px">
          <el-form-item label="回复">
            <el-input v-model="replyForm.admin_reply" type="textarea" :rows="4" />
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="replyForm.status" style="width: 100%">
              <el-option v-for="(label, val) in STATUS" :key="val" :label="label" :value="val" />
            </el-select>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="replyVisible = false">取消</el-button>
        <el-button type="primary" :loading="replying" @click="submitReply">保存</el-button>
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

.feedback-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.filter-card { margin-bottom: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
