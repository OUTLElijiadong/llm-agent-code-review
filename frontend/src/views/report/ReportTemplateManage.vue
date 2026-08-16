<template>
  <div class="template-manage-page">
    <!-- ============ 页面头部 ============ -->
    <div class="page-header">
      <div class="page-title">
        <h2>报告模板管理</h2>
        <p class="page-desc">管理报告模板(Jinja2 语法),内置模板可编辑内容但不可删除</p>
      </div>
      <div class="page-actions">
        <el-button @click="$router.push('/reports')">
          <el-icon><ArrowLeft /></el-icon>返回报告列表
        </el-button>
        <el-button type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>新建模板
        </el-button>
      </div>
    </div>

    <!-- ============ 筛选栏 ============ -->
    <el-card shadow="hover" class="filter-card">
      <div class="filter-bar">
        <el-select
          v-model="filterType"
          placeholder="全部类型"
          clearable
          style="width: 200px"
          @change="loadData"
        >
          <el-option
            v-for="opt in typeOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="搜索模板名称"
          clearable
          style="width: 240px"
          @change="loadData"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
      </div>
    </el-card>

    <!-- ============ 模板列表 ============ -->
    <el-card shadow="hover">
      <el-table
        :data="filteredTemplates"
        v-loading="loading"
        style="width: 100%"
      >
        <template #empty>
          <EmptyState description="还没有自定义模板" action-text="新建模板" @action="openCreateDialog()" />
        </template>
        <el-table-column prop="name" label="模板名称" min-width="180">
          <template #default="{ row }">
            <div class="cell-name">
              <span class="name-text">{{ row.name }}</span>
              <el-tag v-if="row.is_builtin === 1" size="small" type="info" effect="plain">内置</el-tag>
              <el-tag v-else size="small" type="success" effect="plain">自定义</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="130">
          <template #default="{ row }">
            <el-tag :type="typeTagType(row.type)" size="small">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.description || '—' }}
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="创建时间" width="170">
          <template #default="{ row }">
            {{ formatDateTime(row.create_time) }}
          </template>
        </el-table-column>
        <el-table-column prop="update_time" label="更新时间" width="170">
          <template #default="{ row }">
            {{ formatDateTime(row.update_time) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openPreviewDialog(row)">
              <el-icon><View /></el-icon>预览
            </el-button>
            <el-button link type="primary" size="small" @click="openEditDialog(row)">
              <el-icon><Edit /></el-icon>编辑
            </el-button>
            <el-button
              link
              type="danger"
              size="small"
              :disabled="row.is_builtin === 1"
              @click="handleDelete(row)"
            >
              <el-icon><Delete /></el-icon>删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 创建/编辑模板对话框 ============ -->
    <el-dialog
      v-model="formDialogVisible"
      :title="formMode === 'create' ? '新建报告模板' : '编辑报告模板'"
      width="780px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form
        ref="formRef"
        :model="formData"
        :rules="formRules"
        label-width="90px"
        label-position="right"
      >
        <el-form-item label="模板名称" prop="name">
          <el-input v-model="formData.name" placeholder="请输入模板名称(1-128 字符)" maxlength="128" show-word-limit />
        </el-form-item>
        <el-form-item label="模板类型" prop="type">
          <el-select v-model="formData.type" placeholder="请选择模板类型" style="width: 100%">
            <el-option
              v-for="opt in typeOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="模板描述" prop="description">
          <el-input
            v-model="formData.description"
            type="textarea"
            :rows="2"
            placeholder="可选,简要描述模板用途(最长 255 字符)"
            maxlength="255"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="模板内容" prop="content">
          <div class="content-editor-wrap">
            <div class="content-editor-tip font-mono" v-pre>
              Jinja2 模板语法,可用变量:{{ project }} / {{ task }} / {{ issues }} / {{ summary }} / {{ score }}
            </div>
            <el-input
              v-model="formData.content"
              type="textarea"
              :rows="14"
              placeholder="请输入 Jinja2 模板内容"
              class="content-editor"
              resize="vertical"
            />
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">
          {{ formMode === 'create' ? '创建' : '保存' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 预览模板对话框 ============ -->
    <el-dialog
      v-model="previewDialogVisible"
      :title="`模板预览: ${previewTemplate?.name ?? ''}`"
      width="860px"
      destroy-on-close
    >
      <div v-if="previewTemplate" class="preview-content">
        <div class="preview-meta">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="模板名称">{{ previewTemplate.name }}</el-descriptions-item>
            <el-descriptions-item label="模板类型">
              <el-tag :type="typeTagType(previewTemplate.type)" size="small">{{ typeLabel(previewTemplate.type) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="是否内置">
              {{ previewTemplate.is_builtin === 1 ? '内置模板' : '自定义模板' }}
            </el-descriptions-item>
            <el-descriptions-item label="描述">{{ previewTemplate.description || '—' }}</el-descriptions-item>
          </el-descriptions>
        </div>
        <div class="preview-source-label font-mono">Jinja2 模板源码</div>
        <pre class="preview-source">{{ previewTemplate.content }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import EmptyState from '@/components/common/EmptyState.vue'
import { ref, computed, onMounted, reactive } from 'vue'
import { type FormInstance, type FormRules } from 'element-plus'
import { ArrowLeft, Plus, Search, View, Edit, Delete } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'
import {
  listTemplates,
  createTemplate,
  updateTemplate,
  deleteTemplate,
} from '@/api/report'
import type {
  ReportTemplate,
  ReportTemplateCreateIn,
  ReportTemplateUpdateIn,
  ReportTemplateType,
} from '@/types/report'

// ============ 状态 ============

/** 模板列表 */
const templates = ref<ReportTemplate[]>([])
/** 列表加载状态 */
const loading = ref(false)
/** 类型筛选 */
const filterType = ref<ReportTemplateType | ''>('')
/** 关键词搜索(模板名称) */
const keyword = ref('')

/** 表单对话框可见性 */
const formDialogVisible = ref(false)
/** 表单模式:create / edit */
const formMode = ref<'create' | 'edit'>('create')
/** 当前编辑的模板 ID(edit 模式) */
const editingId = ref<number | null>(null)
/** 提交中状态 */
const submitting = ref(false)
/** 表单引用 */
const formRef = ref<FormInstance>()
/** 表单数据 */
const formData = reactive<{
  name: string
  type: ReportTemplateType
  content: string
  description: string
}>({
  name: '',
  type: 'detailed',
  content: '',
  description: '',
})

/** 表单校验规则 */
const formRules: FormRules = {
  name: [
    { required: true, message: '请输入模板名称', trigger: 'blur' },
    { min: 1, max: 128, message: '模板名称长度 1-128 字符', trigger: 'blur' },
  ],
  type: [
    { required: true, message: '请选择模板类型', trigger: 'change' },
  ],
  content: [
    { required: true, message: '请输入模板内容', trigger: 'blur' },
    { min: 1, message: '模板内容不能为空', trigger: 'blur' },
  ],
}

/** 预览对话框可见性 */
const previewDialogVisible = ref(false)
/** 当前预览的模板对象 */
const previewTemplate = ref<ReportTemplate | null>(null)

/** 模板类型选项(simple/detailed/compliance/custom) */
const typeOptions: Array<{ label: string; value: ReportTemplateType }> = [
  { label: '简洁模板 (Simple)', value: 'simple' },
  { label: '详细模板 (Detailed)', value: 'detailed' },
  { label: '合规模板 (Compliance)', value: 'compliance' },
  { label: '自定义模板 (Custom)', value: 'custom' },
]

// ============ 计算属性 ============

/** 按类型与关键词过滤后的模板列表 */
const filteredTemplates = computed<ReportTemplate[]>(() => {
  let list = templates.value
  if (filterType.value) {
    list = list.filter((t) => t.type === filterType.value)
  }
  if (keyword.value.trim()) {
    const kw = keyword.value.trim().toLowerCase()
    list = list.filter((t) => t.name.toLowerCase().includes(kw))
  }
  return list
})

// ============ 工具函数 ============

/**
 * 格式化日期时间展示。
 * @param s - ISO 8601 时间字符串
 * @returns 格式化后的字符串 YYYY-MM-DD HH:mm
 */
function formatDateTime(s: string): string {
  if (!s) return '—'
  return dayjs(s).format('YYYY-MM-DD HH:mm')
}

/**
 * 返回模板类型的中文标签。
 * @param type - 模板类型
 * @returns 中文标签
 */
function typeLabel(type: ReportTemplateType): string {
  const opt = typeOptions.find((o) => o.value === type)
  return opt ? opt.label : type
}

/**
 * 返回模板类型对应的 el-tag 类型(颜色)。
 * @param type - 模板类型
 * @returns el-tag type 属性值
 */
function typeTagType(type: ReportTemplateType): 'primary' | 'success' | 'warning' | 'info' {
  const map: Record<ReportTemplateType, 'primary' | 'success' | 'warning' | 'info'> = {
    simple: 'info',
    detailed: 'primary',
    compliance: 'warning',
    custom: 'success',
  }
  return map[type] ?? 'info'
}

// ============ 数据加载 ============

/**
 * 加载模板列表。
 * 后端 listTemplates 返回全部模板(含内置与自定义),前端再做类型筛选。
 */
async function loadData(): Promise<void> {
  loading.value = true
  try {
    templates.value = await listTemplates()
  } catch {
    // http 拦截器已统一报错
    templates.value = []
  } finally {
    loading.value = false
  }
}

// ============ 创建 / 编辑 ============

/**
 * 重置表单数据为初始值。
 */
function resetForm(): void {
  formData.name = ''
  formData.type = 'detailed'
  formData.content = ''
  formData.description = ''
  editingId.value = null
  formRef.value?.clearValidate()
}

/**
 * 打开创建模板对话框。
 */
function openCreateDialog(): void {
  formMode.value = 'create'
  resetForm()
  formDialogVisible.value = true
}

/**
 * 打开编辑模板对话框。
 * @param row - 当前行的模板对象
 */
function openEditDialog(row: ReportTemplate): void {
  formMode.value = 'edit'
  editingId.value = row.id
  formData.name = row.name
  formData.type = row.type
  formData.content = row.content
  formData.description = row.description ?? ''
  formDialogVisible.value = true
  formRef.value?.clearValidate()
}

/**
 * 提交表单(创建或更新模板)。
 * 先校验表单,通过后根据 formMode 调用对应 API。
 */
async function handleSubmit(): Promise<void> {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return // 校验失败
  }

  submitting.value = true
  try {
    if (formMode.value === 'create') {
      const payload: ReportTemplateCreateIn = {
        name: formData.name,
        type: formData.type,
        content: formData.content,
        description: formData.description || undefined,
      }
      await createTemplate(payload)
      ElMessage.success('模板创建成功')
    } else if (editingId.value !== null) {
      const payload: ReportTemplateUpdateIn = {
        name: formData.name,
        type: formData.type,
        content: formData.content,
        description: formData.description || undefined,
      }
      await updateTemplate(editingId.value, payload)
      ElMessage.success('模板更新成功')
    }
    formDialogVisible.value = false
    await loadData()
  } catch {
    // http 拦截器已统一报错
  } finally {
    submitting.value = false
  }
}

// ============ 删除 ============

/**
 * 删除模板(带二次确认,内置模板按钮已禁用,此处再防御性校验)。
 * @param row - 当前行的模板对象
 */
async function handleDelete(row: ReportTemplate): Promise<void> {
  // 防御性校验:内置模板不可删除
  if (row.is_builtin === 1) {
    ElMessage.warning('内置模板不可删除')
    return
  }
  const ok = await confirmDanger({ target: `删除模板「${row.name}」` })
  if (!ok) return
  try {
    await deleteTemplate(row.id)
    ElMessage.success('模板已删除')
    await loadData()
  } catch {
    /* http 拦截器已处理 */
  }
}

// ============ 预览 ============

/**
 * 打开预览对话框,展示模板元信息与 Jinja2 源码。
 * @param row - 当前行的模板对象
 */
function openPreviewDialog(row: ReportTemplate): void {
  previewTemplate.value = row
  previewDialogVisible.value = true
}

// ============ 生命周期 ============

onMounted(() => {
  loadData()
})
</script>

<style scoped lang="scss">
.template-manage-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ============ 页面头部 ============ */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-title {
  h2 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    color: var(--gray-900);
  }

  .page-desc {
    margin: 6px 0 0;
    font-size: 12.5px;
    color: var(--gray-500);
  }
}

.page-actions {
  display: flex;
  gap: 8px;
}

/* ============ 筛选栏 ============ */
.filter-card {
  :deep(.el-card__body) {
    padding: 16px;
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

/* ============ 列表单元格 ============ */
.cell-name {
  display: flex;
  align-items: center;
  gap: 8px;

  .name-text {
    font-weight: 500;
    color: var(--gray-800);
  }
}

/* ============ 模板内容编辑器 ============ */
.content-editor-wrap {
  width: 100%;
}

.content-editor-tip {
  margin-bottom: 6px;
  padding: 6px 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--gray-600);
  line-height: 1.6;
}

.content-editor {
  :deep(.el-textarea__inner) {
    font-family: var(--font-mono, 'SFMono-Regular', Consolas, monospace);
    font-size: 12.5px;
    line-height: 1.7;
  }
}

/* ============ 预览对话框 ============ */
.preview-content {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.preview-meta {
  :deep(.el-descriptions) {
    --el-descriptions-item-bordered-label-background: var(--gray-50);
  }
}

.preview-source-label {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.preview-source {
  margin: 0;
  padding: 14px 16px;
  background: #F6F8FA;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  font-family: var(--font-mono, 'SFMono-Regular', Consolas, monospace);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--gray-800);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 480px;
  overflow: auto;
}
</style>
