<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
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
const loading = ref(false)
const submitting = ref(false)

const submitVisible = ref(false)
const form = reactive({ feedback_type: 'suggestion', content: '', contact: '' })

const replyVisible = ref(false)
const current = ref<Feedback | null>(null)
const replyForm = reactive({ admin_reply: '', status: 'replied' })

async function load() {
  loading.value = true
  try {
    const res = await getFeedbackList({
      page: page.value, page_size: pageSize.value,
      scope: isAdmin.value ? scope.value : 'mine',
    })
    list.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (!form.content.trim()) {
    ElMessage.warning('请填写反馈内容')
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    await createFeedback({ ...form })
    ElMessage.success('反馈已提交,感谢你的建议')
    submitVisible.value = false
    Object.assign(form, { feedback_type: 'suggestion', content: '', contact: '' })
    page.value = 1
    load()
  } finally {
    submitting.value = false
  }
}

function openReply(f: Feedback) {
  current.value = f
  Object.assign(replyForm, { admin_reply: f.admin_reply || '', status: 'replied' })
  replyVisible.value = true
}

async function submitReply() {
  if (!current.value) return
  await replyFeedback(current.value.id, { ...replyForm })
  ElMessage.success('已回复')
  replyVisible.value = false
  load()
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
        <el-radio-button label="mine">我的反馈</el-radio-button>
        <el-radio-button label="all">全部反馈</el-radio-button>
      </el-radio-group>
    </el-card>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="list" style="width: 100%">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">{{ TYPE[row.feedback_type] || row.feedback_type }}</template>
        </el-table-column>
        <el-table-column prop="content" label="内容" min-width="220" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_TAG[row.status] as any">{{ STATUS[row.status] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="170">
          <template #default="{ row }">{{ formatDate(row.create_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button v-if="isAdmin" link type="primary" size="small" @click="openReply(row)">回复</el-button>
            <el-popover v-if="row.admin_reply" placement="left" width="320" trigger="click">
              <template #reference><el-button link size="small">管理员回复</el-button></template>
              <p style="white-space: pre-wrap; margin: 0">{{ row.admin_reply }}</p>
            </el-popover>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; load() }" />
      </div>
    </el-card>

    <el-dialog v-model="submitVisible" title="提交反馈" width="540px">
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
        <el-button type="primary" @click="submitReply">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.feedback-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.filter-card { margin-bottom: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
