<template>
  <div class="review-start-page">
    <div class="page-header">
      <h2>启动代码审查</h2>
    </div>

    <el-card shadow="hover" class="form-card">
      <PrismLoading v-if="loadingProjects" compact label="正在加载项目范围" :sublabel="projectsProgress" />
      <el-alert v-if="projectsError" type="error" :closable="false" show-icon :title="projectsError">
        <el-button :disabled="loadingProjects || submitting" @click="loadProjects">重试项目加载</el-button>
      </el-alert>
      <el-form ref="formRef" :model="form" :rules="formRules" :disabled="submitting" label-width="100px" size="default">
        <el-form-item label="审查名称">
          <el-input v-model="form.task_name" placeholder="可选，输入任务名称" :maxlength="100" show-word-limit />
        </el-form-item>

        <el-form-item label="审查范围">
          <el-radio-group v-model="form.scope" @change="onScopeChange">
            <el-radio-button value="whole" :disabled="form.review_type === 'discuss'"><el-icon><Box /></el-icon> 整个项目</el-radio-button>
            <el-radio-button value="files"><el-icon><FolderOpened /></el-icon> 指定文件</el-radio-button>
            <el-radio-button value="all" :disabled="form.review_type === 'discuss'"><el-icon><Connection /></el-icon> 全部项目</el-radio-button>
          </el-radio-group>
          <div class="scope-hint">
            <template v-if="form.scope === 'whole'">读取完整文件范围，审查全部有效非空文本文件；空标记文件保留，不参与扫描。</template>
            <template v-else-if="form.scope === 'files'">手动选择有效非空文件；二进制文件不参与文本扫描。</template>
            <template v-else>为每个活跃项目创建后台任务；逐项校验完整范围，超限或失败不截断、不冒充无文件。</template>
          </div>
        </el-form-item>

        <el-form-item v-if="form.scope !== 'all'" label="选择项目" prop="project_id">
          <el-select
            v-model="form.project_id"
            placeholder="请选择审查项目"
            style="width: 100%"
            filterable
            @change="onProjectChange"
            :loading="loadingProjects"
            :disabled="loadingProjects || Boolean(projectsError)"
          >
            <el-option
              v-for="p in projects"
              :key="p.id"
              :label="p.project_name"
              :value="p.id"
            />
          </el-select>
        </el-form-item>

        <el-alert v-if="form.scope !== 'all' && filesError" type="error" :closable="false" show-icon :title="filesError">
          <el-button :disabled="loadingFiles || submitting" @click="onProjectChange(form.project_id)">重试文件加载</el-button>
        </el-alert>

        <el-form-item v-if="form.scope === 'all'" label="项目范围">
          <div class="all-projects-summary">
            将审查全部 <b>{{ projects.length }}</b> 个活跃项目
            <span v-if="projects.length === 0" class="form-hint">（暂无活跃项目）</span>
          </div>
        </el-form-item>

        <!-- 指定文件：手动勾选 -->
        <el-form-item v-if="form.scope === 'files'" label="选择文件" prop="file_ids">
          <div v-if="!form.project_id" class="form-hint">请先选择项目</div>
          <PrismLoading
            v-else-if="loadingFiles"
            compact
            label="正在加载代码文件"
            :sublabel="filesProgress"
          />
          <template v-else-if="!filesError">
            <div v-if="form.review_type !== 'discuss'" class="file-toolbar">
              <el-checkbox
                :model-value="isAllSelected"
                :indeterminate="isIndeterminate"
                :disabled="reviewableFiles.length === 0 || reviewableFiles.length > MAX_FILES"
                class="select-all-checkbox"
                @change="toggleSelectAll"
              >
                全选
              </el-checkbox>
              <span class="file-count-text">已选 {{ form.file_ids.length }} / {{ reviewableFiles.length }} 个有效文件；共 {{ files.length }} 个文本文件，排除 {{ excludedFiles.length }} 个</span>
            </div>
            <el-radio-group v-if="form.review_type === 'discuss'" v-model="singleFileId" class="file-list">
              <div v-for="file in files" :key="file.id" class="file-item">
                <el-radio :value="file.id" :disabled="!isReviewable(file)">
                  {{ file.file_name }} · {{ formatSize(file.size_bytes) }}
                  <span v-if="!isReviewable(file)">（{{ fileExclusionReason(file) }}）</span>
                </el-radio>
              </div>
              <EmptyState v-if="files.length === 0" description="该项目暂无代码文件" :image-size="80" />
            </el-radio-group>
            <el-checkbox-group v-else v-model="form.file_ids" :max="MAX_FILES">
              <div class="file-list">
                <div
                  v-for="f in files"
                  :key="f.id"
                  class="file-item"
                >
                  <el-checkbox :value="f.id" :disabled="!isReviewable(f)">
                    <span class="file-name">{{ f.file_name }}</span>
                    <el-tag size="small" type="info" class="file-lang">{{ f.language }}</el-tag>
                    <span class="file-size">{{ formatSize(f.size_bytes) }}</span>
                    <span v-if="!isReviewable(f)">（{{ fileExclusionReason(f) }}）</span>
                  </el-checkbox>
                </div>
                <EmptyState v-if="files.length === 0" description="该项目暂无代码文件" :image-size="80" />
              </div>
            </el-checkbox-group>
          </template>
        </el-form-item>

        <!-- 整个项目：只显示统计，不逐个勾选 -->
        <el-form-item v-else-if="form.scope === 'whole'" label="文件范围">
          <div v-if="!form.project_id" class="form-hint">请先选择项目</div>
          <PrismLoading
            v-else-if="loadingFiles"
            compact
            label="正在加载代码文件"
            :sublabel="filesProgress"
          />
          <EmptyState
            v-else-if="!filesError && files.length === 0"
            compact
            description="该项目还没有可审查的代码文件"
            action-text="去项目上传文件"
            :action-to="form.project_id ? `/projects/${form.project_id}` : '/projects'"
          />
          <div v-else-if="!filesError" class="whole-summary">
            完整读取 <b>{{ files.length }}</b> 个文本文件，其中有效非空 <b>{{ reviewableFiles.length }}</b> 个，排除 <b>{{ excludedFiles.length }}</b> 个。
            <ul v-if="excludedFiles.length">
              <li v-for="file in excludedFiles" :key="file.id">{{ file.file_name }}：{{ fileExclusionReason(file) }}</li>
            </ul>
          </div>
        </el-form-item>

        <el-form-item label="审查类型">
          <el-radio-group v-model="form.review_type" @change="onReviewTypeChange">
            <el-radio value="quick"><el-icon><Lightning /></el-icon> 快速审查</el-radio>
            <el-radio value="standard"><el-icon><DocumentChecked /></el-icon> 标准审查</el-radio>
            <el-radio value="security"><el-icon><Aim /></el-icon> 安全代理（渗透/漏洞）</el-radio>
            <el-radio value="performance"><el-icon><Odometer /></el-icon> 性能代理</el-radio>
            <el-radio value="full"><el-icon><Lightning /></el-icon> 多Agent全面审查(并行)</el-radio>
            <el-radio v-if="form.scope !== 'all'" value="discuss">
              <el-icon><ChatDotRound /></el-icon> 多Agent圆桌讨论(实时可见每个Agent的思考)
            </el-radio>
          </el-radio-group>
          <div v-if="form.scope === 'all'" class="scope-hint">
            全部项目模式为批量后台审查，暂不支持实时圆桌讨论。
          </div>
          <div v-if="form.review_type === 'discuss'" class="scope-hint">圆桌讨论仅支持单文件，请明确选择一个有效非空文件；不会自动取第一项。</div>
        </el-form-item>

        <el-alert v-if="selectionIssue" type="warning" :closable="false" show-icon :title="selectionIssue" />
        <el-alert v-if="submissionError" type="error" :closable="false" show-icon :title="submissionError" />

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="onSubmit" :disabled="submitDisabled">
            {{ submitLabel }}
          </el-button>
          <el-button :disabled="submitting" @click="onReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="batchResults.length" shadow="never" class="form-card batch-results">
      <div role="status" aria-live="polite">已处理 {{ batchCompleted }} / {{ batchResults.length }} · 成功 {{ batchCreated }} · 失败 {{ batchFailed }} · 跳过 {{ batchSkipped }}</div>
      <PrismLoading v-if="batchProcessing" compact :label="submissionStage" :sublabel="reviewingSublabel" />
      <p v-if="batchProcessing">离开本页会停止尚未发出的创建请求；已发出请求以任务列表为准。</p>
      <ul>
        <li v-for="result in batchResults" :key="result.project.id">
          <b>{{ result.project.project_name }}</b>：{{ batchStatusLabel[result.status] }} · {{ result.message }}
          <el-button v-if="result.taskId" link @click="router.push(`/reviews/${result.taskId}`)">查看任务 #{{ result.taskId }}</el-button>
        </li>
      </ul>
      <el-button :disabled="submitting || !batchResults.some(result => result.retryable)" @click="retryBatchFailures">仅重试读取失败的项目</el-button>
      <el-button @click="router.push('/reviews')">查看任务列表</el-button>
    </el-card>

    <el-dialog
      v-model="reviewingVisible"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
      width="420px" top="30vh" destroy-on-close
    >
      <template #header>
        <div class="reviewing-header">
          <span class="reviewing-icon">🔍</span>
          <span>{{ submissionStage }}</span>
        </div>
      </template>
      <PrismLoading :label="submissionStage" :sublabel="reviewingSublabel" compact />
      <div class="reviewing-hint">提交阶段不代表扫描完成；任务创建后前往详情查看真实状态。</div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import EmptyState from '@/components/common/EmptyState.vue'
import { confirmDanger } from '@/composables/useDangerConfirm'

import type { FormInstance, FormRules } from 'element-plus'
import { Aim, Box, ChatDotRound, Connection, DocumentChecked, FolderOpened, Lightning, Odometer } from '@element-plus/icons-vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getProjects } from '@/api/project'
import { list as getCodeFiles } from '@/api/codeFile'
import { startReview } from '@/api/review'
import { startDiscussion } from '@/api/discussion'
import type { ProjectOut, CodeFileOut } from '@/types/project'
import type { Page } from '@/types/common'
import { ElMessage } from 'element-plus/es/components/message/index'

const MAX_FILES = 500

const router = useRouter()
const formRef = ref<FormInstance>()
const submitting = ref(false)
const reviewingVisible = ref(false)
const reviewingSublabel = ref('')
const submissionStage = ref('')
const submissionError = ref('')
const loadingProjects = ref(false)
const loadingFiles = ref(false)
const projectsError = ref('')
const filesError = ref('')
const projectsProgress = ref('等待读取项目总数')
const filesProgress = ref('等待读取文件总数')
const projects = ref<ProjectOut[]>([])
const files = ref<CodeFileOut[]>([])
const filesProjectId = ref<number | null>(null)
let projectsRequest = 0
let filesRequest = 0
let disposed = false

const form = reactive({
  task_name: '',
  scope: 'whole' as 'whole' | 'files' | 'all',
  project_id: null as number | null,
  file_ids: [] as number[],
  review_type: 'standard',
})

/**
 * 表单必填校验:项目与文件按审查范围动态校验(scope=all 时跳过)。
 * 提交按钮本身有 submitDisabled 兜底,rules 用于给出字段级红字反馈。
 */
const formRules: FormRules = {
  project_id: [
    {
      validator: (_rule, value, callback) => {
        if (form.scope !== 'all' && (value === null || value === undefined || value === '')) {
          callback(new Error('请选择审查项目'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
  file_ids: [
    {
      validator: (_rule, value: number[], callback) => {
        if (form.scope === 'files' && (!value || value.length === 0)) {
          callback(new Error('请至少选择一个文件'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function errorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') {
    return error.message || fallback
  }
  return fallback
}

function isReviewable(file: CodeFileOut): boolean {
  return file.is_reviewable === true && file.size_bytes > 0 && !file.is_binary
}

function fileExclusionReason(file: CodeFileOut): string {
  if (file.is_binary) return '二进制文件不参与文本扫描'
  if (file.size_bytes === 0) return '空文件，保留存储但不参与扫描'
  if (file.is_reviewable === false) return '空白或无有效审查内容，保留存储但不参与扫描'
  return '接口未确认可审查状态，请刷新或检查后端字段'
}

const reviewableFiles = computed(() => files.value.filter(isReviewable))
const excludedFiles = computed(() => files.value.filter(file => !isReviewable(file)))
const isAllSelected = computed(() => reviewableFiles.value.length > 0
  && form.file_ids.length === reviewableFiles.value.length
  && reviewableFiles.value.every(file => form.file_ids.includes(file.id)))
const isIndeterminate = computed(() => form.file_ids.length > 0 && !isAllSelected.value)
const singleFileId = computed({
  get: () => form.file_ids.length === 1 ? form.file_ids[0] : undefined,
  set: (fileId: number | undefined) => { form.file_ids = fileId === undefined ? [] : [fileId] },
})

function toggleSelectAll(selected: boolean | string | number) {
  if (submitting.value || form.review_type === 'discuss') return
  if (selected && reviewableFiles.value.length > MAX_FILES) return
  form.file_ids = selected ? reviewableFiles.value.map(file => file.id) : []
}

const submitLabel = computed(() => {
  if (form.scope === 'all') return '启动全部项目审查'
  return form.review_type === 'discuss' ? '启动讨论审查' : '启动审查'
})

const selectionIssue = computed(() => {
  if (loadingProjects.value) return '项目范围尚未读取完成，请稍候'
  if (projectsError.value) return '项目范围读取失败，请重试，不会使用不完整范围'
  if (form.scope === 'all') {
    if (form.review_type === 'discuss') return '圆桌讨论仅支持单文件'
    return projects.value.length ? '' : '暂无活跃项目'
  }
  if (!form.project_id) return '请选择项目'
  if (loadingFiles.value) return '文件范围尚未读取完成，请稍候'
  if (filesError.value || filesProjectId.value !== form.project_id) return '请重新加载所选项目的完整文件范围'
  if (form.review_type === 'discuss' && (form.scope !== 'files' || form.file_ids.length !== 1)) {
    return '圆桌讨论仅支持单文件，请明确选择一个有效非空文件'
  }
  if (form.scope === 'whole' && files.value.some(file => file.size_bytes > 0 && !file.is_binary && typeof file.is_reviewable !== 'boolean')) {
    return '部分文件缺少可审查状态，无法确认完整扫描范围，请刷新或检查后端字段'
  }
  const selectedIds = form.scope === 'whole' ? reviewableFiles.value.map(file => file.id) : form.file_ids
  if (!selectedIds.length) return form.scope === 'whole' ? '该项目无有效非空文件可供审查' : '请选择有效非空文件'
  if (selectedIds.length > MAX_FILES) return `有效文件 ${selectedIds.length} 个，超过单任务上限 ${MAX_FILES}；请改为指定文件并明确选择范围，不会自动截断`
  if (new Set(selectedIds).size !== selectedIds.length
    || selectedIds.some(fileId => !reviewableFiles.value.some(file => file.id === fileId && file.project_id === form.project_id))) {
    return '所选文件不属于当前项目或没有有效内容，请重新选择'
  }
  return ''
})

const submitDisabled = computed(() => submitting.value || Boolean(selectionIssue.value))

async function readCompletePages<Item extends { id: number }>(
  fetchPage: (page: number) => Promise<Page<Item>>,
  progress: (loaded: number, total: number) => void,
  current: () => boolean = () => !disposed,
): Promise<Item[]> {
  const items: Item[] = []
  const ids = new Set<number>()
  let expectedTotal: number | undefined
  for (let page = 1; current(); page++) {
    const data = await fetchPage(page)
    if (!current()) return []
    if (!Array.isArray(data.items) || !Number.isSafeInteger(data.total) || data.total < 0
      || (expectedTotal !== undefined && expectedTotal !== data.total)
      || (data.page !== undefined && data.page !== page)) {
      throw new Error('分页范围发生变化或响应不完整，请重新加载；未使用部分结果')
    }
    expectedTotal = data.total
    for (const item of data.items) {
      if (!Number.isSafeInteger(item.id) || ids.has(item.id)) {
        throw new Error('分页包含重复或无效记录，请重新加载；未使用部分结果')
      }
      ids.add(item.id)
      items.push(item)
    }
    if (items.length > expectedTotal || (!data.items.length && items.length < expectedTotal)) {
      throw new Error('分页提前结束或总数不一致，请重新加载；未使用部分结果')
    }
    progress(items.length, expectedTotal)
    if (items.length === expectedTotal) return items
  }
  return []
}

async function loadProjects() {
  if (submitting.value) return
  const request = ++projectsRequest
  const current = () => !disposed && request === projectsRequest
  loadingProjects.value = true
  projectsError.value = ''
  projects.value = []
  projectsProgress.value = '等待读取项目总数'
  try {
    const items = await readCompletePages(
      page => getProjects({ page, page_size: 100, status: 'active' }),
      (loaded, total) => { projectsProgress.value = `已读取 ${loaded} / ${total} 个项目` },
      current,
    )
    if (current()) projects.value = items.filter(project => project.status === 'active')
  } catch (error) {
    if (current()) projectsError.value = errorMessage(error, '项目范围读取失败，请重试')
  } finally {
    if (current()) loadingProjects.value = false
  }
}

async function onProjectChange(projectId: number | null) {
  if (submitting.value) return
  const request = ++filesRequest
  const current = () => !disposed && request === filesRequest && form.project_id === projectId
  form.file_ids = []
  files.value = []
  filesProjectId.value = null
  filesError.value = ''
  submissionError.value = ''
  loadingFiles.value = false
  if (!projectId) return
  loadingFiles.value = true
  filesProgress.value = '等待读取文件总数'
  try {
    const items = await readCompletePages(
      page => getCodeFiles({ project_id: projectId, page, page_size: 500, exclude_binary: true }),
      (loaded, total) => { filesProgress.value = `已读取 ${loaded} / ${total} 个文本文件` },
      current,
    )
    if (!current()) return
    if (items.some(file => file.project_id !== projectId)) throw new Error('文件与所选项目不匹配，请重新加载')
    files.value = items
    filesProjectId.value = projectId
  } catch (error) {
    if (current()) filesError.value = errorMessage(error, '文件范围读取失败，请重试')
  } finally {
    if (current()) loadingFiles.value = false
  }
}

function onReviewTypeChange() {
  if (form.review_type === 'discuss') {
    form.scope = 'files'
    form.file_ids = []
  }
  submissionError.value = ''
  formRef.value?.clearValidate()
}

function onScopeChange() {
  // 圆桌讨论是单文件实时模式，全部项目模式下不适用，回退到标准审查
  if (form.scope === 'all' && form.review_type === 'discuss') {
    form.review_type = 'standard'
  }
  // 切换范围后清掉旧的字段级校验红字(如「请选择审查项目」)
  formRef.value?.clearValidate()
}

async function submitSingleProject(): Promise<void> {
  if (selectionIssue.value) throw new Error(selectionIssue.value)
  const fileIds = form.scope === 'whole' ? reviewableFiles.value.map(file => file.id) : [...form.file_ids]
  submissionStage.value = form.review_type === 'discuss' ? '正在创建单文件圆桌会话' : '正在提交审查任务'
  reviewingSublabel.value = `提交 ${fileIds.length} 个有效非空文件，等待服务器确认`
  reviewingVisible.value = true

  if (form.review_type === 'discuss') {
    const data = await startDiscussion({
      project_id: form.project_id!,
      file_id: fileIds[0],
      review_type: 'full',
    })
    if (disposed) return
    await router.push({
      name: 'AgentCenter',
      query: {
        discuss_session: data.session_id,
        discuss_ws: data.ws_url,
        discuss_agents: JSON.stringify(data.agents),
        discuss_file: data.file_name,
      },
    })
    return
  }

  const res = await startReview({
    project_id: form.project_id!,
    file_ids: fileIds,
    review_type: form.review_type,
    task_name: form.task_name || undefined,
  })
  if (disposed) return
  submissionStage.value = res?.status === 'pending' ? '任务已排队' : '任务已创建'
  reviewingSublabel.value = '前往任务详情查看后台实际执行状态'
  await router.push(res?.task_id ? `/reviews/${res.task_id}` : '/reviews')
}

interface BatchResult {
  project: ProjectOut
  status: 'pending' | 'loading' | 'submitting' | 'created' | 'skipped' | 'failed'
  message: string
  taskId?: number
  retryable: boolean
}

const batchResults = ref<BatchResult[]>([])
const batchProcessing = ref(false)
const batchCreated = computed(() => batchResults.value.filter(result => result.status === 'created').length)
const batchFailed = computed(() => batchResults.value.filter(result => result.status === 'failed').length)
const batchSkipped = computed(() => batchResults.value.filter(result => result.status === 'skipped').length)
const batchCompleted = computed(() => batchCreated.value + batchFailed.value + batchSkipped.value)
const batchStatusLabel: Record<BatchResult['status'], string> = {
  pending: '待处理', loading: '读取完整范围', submitting: '等待创建确认',
  created: '任务已创建', skipped: '已跳过', failed: '失败',
}
let batchInput = { review_type: 'standard', task_name: '' }

async function processBatch(results: BatchResult[]): Promise<void> {
  for (const result of results) {
    if (disposed) break
    result.status = 'loading'
    result.retryable = false
    result.message = '等待读取文件总数'
    submissionStage.value = '正在读取完整项目范围'
    reviewingSublabel.value = result.project.project_name
    let creating = false
    try {
      const items = await readCompletePages(
        page => getCodeFiles({ project_id: result.project.id, page, page_size: 500, exclude_binary: true }),
        (loaded, total) => { result.message = `已读取 ${loaded} / ${total} 个文本文件` },
      )
      if (disposed) break
      if (items.some(file => file.project_id !== result.project.id)) throw new Error('文件与项目不匹配，未创建任务')
      if (items.some(file => file.size_bytes > 0 && !file.is_binary && typeof file.is_reviewable !== 'boolean')) {
        throw new Error('部分文件缺少可审查状态，未创建任务')
      }
      const validFiles = items.filter(isReviewable)
      const excluded = items.filter(file => !isReviewable(file))
      const coverage = `有效 ${validFiles.length} / ${items.length} 个文本文件` + (excluded.length
        ? `；排除 ${excluded.length} 个：${excluded.map(file => `${file.file_name}（${fileExclusionReason(file)}）`).join('、')}` : '')
      if (!validFiles.length) {
        result.status = 'skipped'
        result.message = `无有效非空文件；${coverage}`
        continue
      }
      if (validFiles.length > MAX_FILES) {
        result.status = 'failed'
        result.message = `${coverage}；超过单任务上限 ${MAX_FILES}，未创建任务；请明确选择文件范围`
        continue
      }
      result.status = 'submitting'
      result.message = coverage
      submissionStage.value = '正在提交审查任务'
      creating = true
      const response = await startReview({
        project_id: result.project.id,
        file_ids: validFiles.map(file => file.id),
        review_type: batchInput.review_type,
        task_name: batchInput.task_name || undefined,
      })
      result.status = 'created'
      result.taskId = response?.task_id
      result.message = `${coverage}；${response?.status === 'pending' ? '已排队' : '已创建，请查看任务状态'}`
    } catch (error) {
      result.status = 'failed'
      result.retryable = !creating
      result.message = `${result.message}；${errorMessage(error, creating ? '创建请求失败' : '文件范围读取失败')}`
        + (creating ? '；请先在任务列表核对创建结果，再决定是否重新提交，避免重复任务' : '；未创建任务，可重试读取')
    }
  }
  submissionStage.value = '批量提交处理结束'
}

async function submitAllProjects(): Promise<void> {
  const selectedProjects = projects.value.map(project => ({ ...project }))
  const ok = await confirmDanger({
    target: `为全部 ${selectedProjects.length} 个项目各创建一个审查任务`,
    consequence: '将串行校验完整范围并创建任务，消耗 AI 审查额度。空白文件排除并列明，超限不截断，失败保留原因。',
    confirmText: '确认批量创建',
  })
  if (!ok || disposed) return
  batchInput = { review_type: form.review_type, task_name: form.task_name }
  batchResults.value = selectedProjects.map(project => ({ project, status: 'pending', message: '等待校验文件范围', retryable: false }))
  batchProcessing.value = true
  try {
    await processBatch(batchResults.value)
  } finally {
    batchProcessing.value = false
  }
  if (disposed) return
  const summary = `成功 ${batchCreated.value}，失败 ${batchFailed.value}，跳过 ${batchSkipped.value}；详情保留在本页`
  if (batchFailed.value) ElMessage.warning(summary)
  else if (batchCreated.value) ElMessage.success(summary)
  else ElMessage.info(summary)
}

async function retryBatchFailures() {
  if (submitting.value) return
  const retryable = batchResults.value.filter(result => result.retryable)
  if (!retryable.length) return
  submitting.value = true
  try {
    const ok = await confirmDanger({
      target: `重新读取 ${retryable.length} 个失败项目的完整文件范围并尝试创建任务`,
      consequence: '只重试未发出创建请求的项目；已创建及提交结果未确认的项目不会重复提交。',
      confirmText: '确认重试',
    })
    if (!ok || disposed) return
    for (const result of retryable) result.status = 'pending'
    batchProcessing.value = true
    await processBatch(retryable)
  } finally {
    batchProcessing.value = false
    submitting.value = false
  }
}

async function onSubmit() {
  if (submitting.value) return
  if (selectionIssue.value) {
    ElMessage.warning(selectionIssue.value)
    return
  }
  submitting.value = true
  submissionError.value = ''
  try {
    if (formRef.value && !await formRef.value.validate().catch(() => false)) return
    if (disposed) return
    if (selectionIssue.value) throw new Error(selectionIssue.value)
    if (form.scope === 'all') {
      await submitAllProjects()
    } else {
      await submitSingleProject()
    }
  } catch (error) {
    submissionError.value = `${errorMessage(error, '提交失败')}；输入已保留。请先核对任务列表或圆桌会话，再重试，避免重复提交。`
  } finally {
    reviewingVisible.value = false
    submitting.value = false
  }
}

function onReset() {
  if (submitting.value) return
  filesRequest++
  files.value = []
  filesProjectId.value = null
  loadingFiles.value = false
  filesError.value = ''
  submissionError.value = ''
  batchResults.value = []
  form.task_name = ''
  form.scope = 'whole'
  form.project_id = null
  form.file_ids = []
  form.review_type = 'standard'
}

onMounted(() => { loadProjects() })

onBeforeUnmount(() => {
  disposed = true
  filesRequest++
  projectsRequest++
})
</script>

<style scoped lang="scss">
.review-start-page { .page-header { margin-bottom: 20px; h2 { margin: 0; font-size: 20px; font-weight: 600; } } }
.form-card { max-width: 800px; }
.scope-hint { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 6px; line-height: 1.5; }
.all-projects-summary, .whole-summary { font-size: 13px; color: var(--el-text-color-primary); b { color: var(--el-color-primary); font-weight: 600; } }
.file-list { max-height: 300px; overflow-y: auto; border: 1px solid var(--el-border-color-lighter); border-radius: 0 0 4px 4px; padding: 8px 12px; width: 100%; }
.file-toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color-lighter); border-bottom: none; border-radius: 4px 4px 0 0; .select-all-checkbox { margin-right: 0; } .file-count-text { font-size: 12px; color: var(--el-text-color-secondary); } }
.file-item { padding: 6px 0; .file-name { font-weight: 500; margin-right: 8px; } .file-lang { margin-right: 8px; } .file-size { font-size: 12px; color: var(--el-text-color-secondary); } }
.form-hint { color: var(--el-text-color-secondary); font-size: 13px; }
.reviewing-header { display: flex; align-items: center; gap: 8px; font-size: 16px; font-weight: 600; .reviewing-icon { font-size: 18px; } }
.reviewing-hint { text-align: center; font-size: 13px; color: var(--el-text-color-secondary); margin-top: 8px; }
.batch-results { margin-top: 16px; ul { max-height: 360px; overflow-y: auto; padding-left: 20px; overflow-wrap: anywhere; } li { margin: 8px 0; } }
</style>
