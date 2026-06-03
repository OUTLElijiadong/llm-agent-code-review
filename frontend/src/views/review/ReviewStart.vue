<template>
  <div class="review-start-page">
    <div class="page-header">
      <h2>启动代码审查</h2>
    </div>

    <el-card shadow="hover" class="form-card">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px" size="default">
        <el-form-item label="审查名称" prop="task_name">
          <el-input v-model="form.task_name" placeholder="可选，输入任务名称" :maxlength="100" show-word-limit />
        </el-form-item>

        <el-form-item label="选择项目" prop="project_id">
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

        <el-form-item label="选择文件" prop="file_ids">
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

        <el-form-item label="审查类型" prop="review_type">
          <el-radio-group v-model="form.review_type">
            <el-radio value="quick">⚡ 快速审查</el-radio>
            <el-radio value="standard">⚡ 标准审查</el-radio>
            <el-radio value="security">⚡ 安全代理</el-radio>
            <el-radio value="performance">⚡ 性能代理</el-radio>
            <el-radio value="full">⚡ 多Agent全面审查(并行)</el-radio>
            <el-radio value="discuss">💬 多Agent圆桌讨论(实时可见每个Agent的思考)</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="onSubmit"
            :disabled="!form.project_id || form.file_ids.length === 0">
            {{ form.review_type === 'discuss' ? '💬 启动讨论审' : '⚡ 启动审查' }}
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
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getProjects } from '@/api/project'
import { list as getCodeFiles } from '@/api/codeFile'
import { startReview } from '@/api/review'
import { startDiscussion } from '@/api/discussion'
import type { ProjectOut, CodeFileOut } from '@/types/project'

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
  project_id: null as number | null,
  file_ids: [] as number[],
  review_type: 'standard',
})

const rules: FormRules = {
  project_id: [{ required: true, message: '请选择项目', trigger: 'change' }],
  file_ids: [{ required: true, type: 'array', min: 1, message: '请至少选择一个文件', trigger: 'change' }],
}

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

async function onSubmit() {
  if (!formRef.value) return
  if (!(await formRef.value.validate().catch(() => false))) return

  submitting.value = true
  try {
    if (form.review_type === 'discuss') {
      const data = await startDiscussion({
        project_id: form.project_id!,
        file_id: form.file_ids[0],
        review_type: 'full',
      })
      router.push({
        name: 'AgentCenter',
        query: {
          discuss_session: data.session_id,
          discuss_agents: JSON.stringify(data.agents),
          discuss_file: data.file_name,
        },
      })
    } else {
      await startReview({
        project_id: form.project_id!,
        file_ids: form.file_ids,
        review_type: form.review_type,
        task_name: form.task_name || undefined,
      })
      reviewingSublabel.value = '已提交至 Agent 调度器'
      reviewingVisible.value = true
      setTimeout(() => { reviewingVisible.value = false; router.push('/reviews') }, 2500)
    }
  } catch { /* interceptor handles */ }
  finally { submitting.value = false }
}

function onReset() {
  formRef.value?.resetFields()
  files.value = []
  form.project_id = null
  form.file_ids = []
  form.review_type = 'standard'
}

onMounted(() => { loadProjects() })
</script>

<style scoped lang="scss">
.review-start-page { .page-header { margin-bottom: 20px; h2 { margin: 0; font-size: 20px; font-weight: 600; } } }
.form-card { max-width: 800px; }
.file-list { max-height: 300px; overflow-y: auto; border: 1px solid var(--el-border-color-lighter); border-radius: 0 0 4px 4px; padding: 8px 12px; width: 100%; }
.file-toolbar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color-lighter); border-bottom: none; border-radius: 4px 4px 0 0; .select-all-checkbox { margin-right: 0; } .file-count-text { font-size: 12px; color: var(--el-text-color-secondary); } }
.file-item { padding: 6px 0; .file-name { font-weight: 500; margin-right: 8px; } .file-lang { margin-right: 8px; } .file-size { font-size: 12px; color: var(--el-text-color-secondary); } }
.form-hint { color: var(--el-text-color-secondary); font-size: 13px; padding: 12px 0; }
.reviewing-header { display: flex; align-items: center; gap: 8px; font-size: 16px; font-weight: 600; .reviewing-icon { font-size: 18px; } }
.reviewing-hint { text-align: center; font-size: 13px; color: var(--el-text-color-secondary); margin-top: 8px; }
</style>
