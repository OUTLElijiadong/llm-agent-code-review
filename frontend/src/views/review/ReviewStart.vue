<template>
  <div class="review-start-page">
    <div class="page-header">
      <h2>启动代码审查</h2>
    </div>

    <el-card shadow="hover" class="form-card">
      <el-form ref="formRef" :model="form" label-width="100px" size="default">
        <el-form-item label="审查名称">
          <el-input v-model="form.task_name" placeholder="可选，输入任务名称" :maxlength="100" show-word-limit />
        </el-form-item>

        <el-form-item label="审查范围">
          <el-radio-group v-model="form.scope" @change="onScopeChange">
            <el-radio-button value="whole">📦 整个项目</el-radio-button>
            <el-radio-button value="files">🗂 指定文件</el-radio-button>
            <el-radio-button value="all">🌐 全部项目</el-radio-button>
          </el-radio-group>
          <div class="scope-hint">
            <template v-if="form.scope === 'whole'">审查所选项目的全部代码文件，无需逐个勾选。</template>
            <template v-else-if="form.scope === 'files'">手动勾选需要审查的文件。</template>
            <template v-else>为你的每个活跃项目各创建一个审查任务，覆盖其全部文件。</template>
          </div>
        </el-form-item>

        <el-form-item v-if="form.scope !== 'all'" label="选择项目">
          <el-select
            v-model="form.project_id"
            placeholder="请选择审查项目"
            style="width: 100%"
            filterable
            @change="onProjectChange"
            :loading="loadingProjects"
          >
            <el-option
              v-for="p in projects"
              :key="p.id"
              :label="p.project_name"
              :value="p.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item v-else label="项目范围">
          <div class="all-projects-summary">
            将审查全部 <b>{{ projects.length }}</b> 个活跃项目
            <span v-if="projects.length === 0" class="form-hint">（暂无活跃项目）</span>
          </div>
        </el-form-item>

        <!-- 指定文件：手动勾选 -->
        <el-form-item v-if="form.scope === 'files'" label="选择文件">
          <div v-if="!form.project_id" class="form-hint">请先选择项目</div>
          <PrismLoading
            v-else-if="loadingFiles"
            compact
            label="正在加载代码文件"
            sublabel="正在读取项目文件列表"
          />
          <template v-else>
            <div class="file-toolbar">
              <el-checkbox
                v-model="isAllSelected"
                :indeterminate="isIndeterminate"
                :disabled="files.length === 0"
                class="select-all-checkbox"
                @change="toggleSelectAll"
              >
                全选
              </el-checkbox>
              <span class="file-count-text">已选 {{ form.file_ids.length }} / {{ files.length }} 个文件</span>
            </div>
            <el-checkbox-group v-model="form.file_ids">
              <div class="file-list">
                <div
                  v-for="f in files"
                  :key="f.id"
                  class="file-item"
                >
                  <el-checkbox :label="f.id" :value="f.id">
                    <span class="file-name">{{ f.file_name }}</span>
                    <el-tag size="small" type="info" class="file-lang">{{ f.language }}</el-tag>
                    <span class="file-size">{{ formatSize(f.size_bytes) }}</span>
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
            sublabel="正在读取项目文件列表"
          />
          <div v-else-if="files.length === 0" class="form-hint">该项目暂无可审查的代码文件</div>
          <div v-else class="whole-summary">
            将审查该项目全部 <b>{{ files.length }}</b> 个文件
            <span v-if="files.length > MAX_FILES" class="form-hint">
              （超过 {{ MAX_FILES }} 个，仅审查前 {{ MAX_FILES }} 个）
            </span>
          </div>
        </el-form-item>

        <el-form-item label="审查类型">
          <el-radio-group v-model="form.review_type">
            <el-radio value="quick">⚡ 快速审查</el-radio>
            <el-radio value="standard">⚡ 标准审查</el-radio>
            <el-radio value="security">🛡 安全代理（渗透/漏洞）</el-radio>
            <el-radio value="performance">⚡ 性能代理</el-radio>
            <el-radio value="full">⚡ 多Agent全面审查(并行)</el-radio>
            <el-radio v-if="form.scope !== 'all'" value="discuss">
              💬 多Agent圆桌讨论(实时可见每个Agent的思考)
            </el-radio>
          </el-radio-group>
          <div v-if="form.scope === 'all'" class="scope-hint">
            全部项目模式为批量后台审查，暂不支持实时圆桌讨论。
          </div>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="onSubmit" :disabled="submitDisabled">
            {{ submitLabel }}
          </el-button>
          <el-button @click="onReset">重置</el-button>
        </el-form-item>
      </el-form>
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
          <span>代码审查已启动</span>
        </div>
      </template>
      <PrismLoading label="正在审查中" :sublabel="reviewingSublabel" compact />
      <div class="reviewing-hint">审查任务已在后台运行，请前往任务列表查看进度</div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'

import type { FormInstance } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getProjects } from '@/api/project'
import { list as getCodeFiles } from '@/api/codeFile'
import { startReview } from '@/api/review'
import { startDiscussion } from '@/api/discussion'
import type { ProjectOut, CodeFileOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'

/** 后端单任务文件数上限 (与 ReviewStartIn.file_ids 校验一致) */
const MAX_FILES = 500

const router = useRouter()
const formRef = ref<FormInstance>()
const submitting = ref(false)
const reviewingVisible = ref(false)
const reviewingSublabel = ref('任务已提交至 Agent 调度器')
const loadingProjects = ref(false)
const loadingFiles = ref(false)
const projects = ref<ProjectOut[]>([])
const files = ref<CodeFileOut[]>([])

const form = reactive({
  task_name: '',
  scope: 'whole' as 'whole' | 'files' | 'all',
  project_id: null as number | null,
  file_ids: [] as number[],
  review_type: 'standard',
})

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const isAllSelected = computed(() => files.value.length > 0 && form.file_ids.length === files.value.length)
const isIndeterminate = computed(() => form.file_ids.length > 0 && form.file_ids.length < files.value.length)
function toggleSelectAll() {
  form.file_ids = isAllSelected.value ? [] : files.value.map((f) => f.id)
}

const submitLabel = computed(() => {
  if (form.scope === 'all') return '🌐 启动全部项目审查'
  return form.review_type === 'discuss' ? '💬 启动讨论审' : '⚡ 启动审查'
})

const submitDisabled = computed(() => {
  if (submitting.value) return true
  if (form.scope === 'all') return projects.value.length === 0
  if (!form.project_id) return true
  if (form.scope === 'whole') return files.value.length === 0
  return form.file_ids.length === 0
})

async function loadProjects() {
  loadingProjects.value = true
  try {
    const data = await getProjects({ page_size: 100 })
    projects.value = data.items.filter((p) => p.status === 'active')
  } finally { loadingProjects.value = false }
}

async function onProjectChange(projectId: number | null) {
  form.file_ids = []
  files.value = []
  if (!projectId) return
  loadingFiles.value = true
  try {
    const data = await getCodeFiles({ project_id: projectId, page_size: 500, exclude_binary: true })
    files.value = data.items
  } finally { loadingFiles.value = false }
}

function onScopeChange() {
  // 圆桌讨论是单文件实时模式，全部项目模式下不适用，回退到标准审查
  if (form.scope === 'all' && form.review_type === 'discuss') {
    form.review_type = 'standard'
  }
}

async function submitSingleProject(): Promise<void> {
  let fileIds = form.scope === 'whole' ? files.value.map((f) => f.id) : [...form.file_ids]
  if (fileIds.length > MAX_FILES) {
    ElMessage.warning(`文件超过 ${MAX_FILES} 个，本次仅审查前 ${MAX_FILES} 个`)
    fileIds = fileIds.slice(0, MAX_FILES)
  }

  if (form.review_type === 'discuss') {
    const data = await startDiscussion({
      project_id: form.project_id!,
      file_id: fileIds[0],
      review_type: 'full',
    })
    router.push({
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
  reviewingSublabel.value = '已提交至 Agent 调度器'
  reviewingVisible.value = true
  // 后端异步执行,跳转任务详情页查看实时进度(无 task_id 时退回列表);
  // 记录定时器,组件卸载时清理,避免卸载后回调操作已销毁组件
  navTimer = setTimeout(() => {
    reviewingVisible.value = false
    router.push(res?.task_id ? `/reviews/${res.task_id}` : '/reviews')
  }, 1500)
}

async function submitAllProjects(): Promise<void> {
  reviewingSublabel.value = `正在为 ${projects.value.length} 个项目创建审查任务…`
  reviewingVisible.value = true
  let created = 0
  let skipped = 0
  for (const p of projects.value) {
    try {
      const data = await getCodeFiles({ project_id: p.id, page_size: 500, exclude_binary: true })
      const ids = data.items.map((f) => f.id).slice(0, MAX_FILES)
      if (ids.length === 0) { skipped++; continue }
      await startReview({
        project_id: p.id,
        file_ids: ids,
        review_type: form.review_type,
        task_name: form.task_name || undefined,
      })
      created++
    } catch { skipped++ }
  }
  reviewingVisible.value = false
  ElMessage.success(
    `已为 ${created} 个项目创建审查任务` + (skipped ? `，跳过 ${skipped} 个（无可审查文件）` : ''),
  )
  router.push('/reviews')
}

async function onSubmit() {
  if (form.scope !== 'all' && !form.project_id) {
    ElMessage.warning('请选择项目')
    return
  }
  if (form.scope === 'files' && form.file_ids.length === 0) {
    ElMessage.warning('请至少选择一个文件')
    return
  }
  if (form.scope === 'whole' && files.value.length === 0) {
    ElMessage.warning('该项目暂无可审查的代码文件')
    return
  }
  if (form.scope === 'all' && projects.value.length === 0) {
    ElMessage.warning('暂无活跃项目')
    return
  }

  submitting.value = true
  try {
    if (form.scope === 'all') {
      await submitAllProjects()
    } else {
      await submitSingleProject()
    }
  } catch { /* interceptor handles */ }
  finally { submitting.value = false }
}

function onReset() {
  files.value = []
  form.task_name = ''
  form.scope = 'whole'
  form.project_id = null
  form.file_ids = []
  form.review_type = 'standard'
}

onMounted(() => { loadProjects() })

// 跳转定时器,卸载时清理
let navTimer: ReturnType<typeof setTimeout> | null = null
onBeforeUnmount(() => {
  if (navTimer) clearTimeout(navTimer)
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
</style>
