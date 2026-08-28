<template>
  <div class="rule-config-page">
    <div class="page-header">
      <h2>审查规则配置</h2>
      <el-button v-if="canCreate" type="primary" size="default" @click="onCreate">
        <el-icon><Plus /></el-icon>新增规则
      </el-button>
    </div>

    <el-card shadow="hover">
      <div class="rule-toolbar">
        <el-input
          v-model="ruleKeyword"
          class="rule-search"
          clearable
          placeholder="搜索规则编码、名称、类型、内容、语言或严重度"
        />
        <span class="rule-count">显示 {{ filteredRules.length }} / {{ rules.length }} 条</span>
      </div>

      <el-table :data="filteredRules" v-loading="loading" style="width: 100%" size="default">
        <el-table-column prop="language" label="语言" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.language === '*' ? 'info' : 'primary'">
              {{ row.language === '*' ? '通用' : row.language }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="rule_code" label="规则编码" width="150" show-overflow-tooltip />
        <el-table-column prop="rule_name" label="规则名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="rule_type" label="规则类型" width="120">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ typeLabel(row.rule_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="severity" label="严重度" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="severityType(row.severity)">{{ row.severity }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="rule_content" label="规则内容" min-width="200" show-overflow-tooltip />
        <el-table-column prop="is_builtin" label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_builtin ? 'success' : 'warning'">
              {{ row.is_builtin ? '内置' : '自定义' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sort_order" label="排序" width="70" />
        <el-table-column prop="enabled" label="状态" width="80">
          <template #default="{ row }">
            <el-switch
              v-if="canToggle(row)"
              v-model="row.enabled"
              :active-value="1"
              :inactive-value="0"
              size="small"
              @change="(val: string | number | boolean) => onToggle(row.id, val as number)"
            />
            <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '已启用' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="canUpdate || canDelete" label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canEdit(row)"
              link
              type="primary"
              size="small"
              @click="onEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              v-if="canRemove(row)"
              link
              type="danger"
              size="small"
              @click="onDelete(row.id)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="isEditing ? '编辑规则' : '新增规则'"
      width="560px"
      @close="resetForm"
    >
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="100px">
        <el-form-item label="规则编码" prop="rule_code">
          <el-input v-model="form.rule_code" :disabled="isEditing" :maxlength="50" placeholder="唯一编码, 如 sql_no_limit" />
        </el-form-item>
        <el-form-item label="规则名称" prop="rule_name">
          <el-input v-model="form.rule_name" :maxlength="100" placeholder="规则名称" />
        </el-form-item>
        <el-form-item label="规则类型" prop="rule_type">
          <el-select v-model="form.rule_type" placeholder="选择规则类型" style="width: 100%">
            <el-option label="安全检查" value="security" />
            <el-option label="潜在Bug" value="correctness" />
            <el-option label="性能优化" value="performance" />
            <el-option label="异常处理" value="robustness" />
            <el-option label="可维护性" value="maintainability" />
            <el-option label="代码风格" value="style" />
            <el-option label="文档注释" value="documentation" />
          </el-select>
        </el-form-item>
        <el-form-item label="适用语言" prop="language">
          <el-select v-model="form.language" placeholder="选择语言" style="width: 100%">
            <el-option label="通用 (*)" value="*" />
            <el-option label="Python" value="python" />
            <el-option label="Java" value="java" />
            <el-option label="TypeScript" value="typescript" />
            <el-option label="Go" value="go" />
            <el-option label="SQL" value="sql" />
          </el-select>
        </el-form-item>
        <el-form-item label="严重度" prop="severity">
          <el-select v-model="form.severity" placeholder="选择严重度" style="width: 100%">
            <el-option label="严重" value="严重" />
            <el-option label="高" value="高" />
            <el-option label="中" value="中" />
            <el-option label="低" value="低" />
          </el-select>
        </el-form-item>
        <el-form-item label="规则内容" prop="rule_content">
          <el-input
            v-model="form.rule_content"
            type="textarea"
            :rows="4"
            placeholder="输入详细的审查规则内容"
            :maxlength="2000"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onSubmit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'

import { Plus } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { getRules, toggleRule, createRule, updateRule, deleteRule } from '@/api/rule'
import type { RuleOut } from '@/types/rule'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const loading = ref(false)
const submitting = ref(false)
const rules = ref<RuleOut[]>([])
const ruleKeyword = ref('')
const dialogVisible = ref(false)
const isEditing = ref(false)
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()
const canCreate = computed(() => userStore.hasPermission('rule:create'))
const canUpdate = computed(() => userStore.hasPermission('rule:update'))
const canDelete = computed(() => userStore.hasPermission('rule:delete'))

const form = reactive({
  rule_code: '',
  rule_name: '',
  rule_type: '',
  rule_content: '',
  language: '*',
  severity: '中',
})

const formRules: FormRules = {
  rule_code: [{ required: true, message: '请输入规则编码', trigger: 'blur' }],
  rule_name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  rule_type: [{ required: true, message: '请选择规则类型', trigger: 'change' }],
  rule_content: [{ required: true, message: '请输入规则内容', trigger: 'blur' }],
}

const typeLabels: Record<string, string> = {
  security: '安全检查',
  correctness: '潜在Bug',
  performance: '性能优化',
  robustness: '异常处理',
  maintainability: '可维护性',
  style: '代码风格',
  documentation: '文档注释',
}

const filteredRules = computed(() => {
  const keyword = ruleKeyword.value.trim().toLowerCase()
  if (!keyword) return rules.value
  const compactKeyword = compactSearchText(keyword)
  return rules.value.filter((rule) => {
    const text = normalizeRuleText(rule)
    return text.includes(keyword) || compactSearchText(text).includes(compactKeyword)
  })
})

/**
 * 汇总规则的可检索字段,用于在大量规则中定位指定规则。
 *
 * @param rule 审查规则记录。
 * @returns 拼接后的规则检索文本。
 */
function normalizeRuleText(rule: RuleOut) {
  return [
    rule.rule_code,
    rule.rule_name,
    rule.rule_type,
    typeLabel(rule.rule_type),
    rule.rule_content,
    rule.language ?? '*',
    rule.severity ?? '',
    rule.is_builtin ? '内置' : '自定义',
  ].join(' ').toLowerCase()
}

/**
 * 移除搜索文本中的分隔符,兼容下划线、短横线被浏览器输入法丢失的场景。
 *
 * @param text 搜索关键词或规则字段文本。
 * @returns 去除非字母数字和中文字符后的紧凑检索文本。
 */
function compactSearchText(text: string) {
  return text.replace(/[^\p{L}\p{N}\u4e00-\u9fa5]/gu, '')
}

function typeLabel(type: string) {
  return typeLabels[type] ?? type
}

function severityType(sev: string) {
  const map: Record<string, string> = { '严重': 'danger', '高': 'warning', '中': 'info', '低': 'primary' }
  return map[sev] ?? 'primary'
}

function canToggle(row: RuleOut): boolean {
  return canUpdate.value && (userStore.isAdmin() || !row.is_builtin)
}

function canEdit(row: RuleOut): boolean {
  return canUpdate.value && !row.is_builtin
}

function canRemove(row: RuleOut): boolean {
  return canDelete.value && !row.is_builtin
}

async function loadRules() {
  loading.value = true
  try {
    rules.value = await getRules()
  } finally {
    loading.value = false
  }
}

async function onToggle(ruleId: number, enabled: number) {
  if (!canUpdate.value) return
  try {
    await toggleRule(ruleId, enabled)
    ElMessage.success(enabled ? '规则已启用' : '规则已禁用')
  } catch {
    loadRules()
  }
}

function onCreate() {
  if (!canCreate.value) return
  isEditing.value = false
  editingId.value = null
  dialogVisible.value = true
}

function onEdit(row: RuleOut) {
  if (!canEdit(row)) return
  isEditing.value = true
  editingId.value = row.id
  form.rule_code = row.rule_code
  form.rule_name = row.rule_name
  form.rule_type = row.rule_type
  form.rule_content = row.rule_content
  form.language = row.language ?? '*'
  form.severity = row.severity ?? '中'
  dialogVisible.value = true
}

async function onDelete(ruleId: number) {
  if (!canDelete.value) return
  try {
    await ElMessageBox.confirm('确定要删除此规则吗？', '确认删除', { type: 'warning' })
    await deleteRule(ruleId)
    ElMessage.success('规则已删除')
    loadRules()
  } catch {
    /* canceled */
  }
}

async function onSubmit() {
  if (isEditing.value ? !canUpdate.value : !canCreate.value) return
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    if (isEditing.value && editingId.value) {
      await updateRule(editingId.value, {
        rule_name: form.rule_name,
        rule_type: form.rule_type,
        rule_content: form.rule_content,
        language: form.language,
        severity: form.severity,
      })
    } else {
      await createRule({
        rule_code: form.rule_code,
        rule_name: form.rule_name,
        rule_type: form.rule_type,
        rule_content: form.rule_content,
        language: form.language,
        severity: form.severity,
      })
    }
    ElMessage.success(isEditing.value ? '规则已更新' : '规则已创建')
    dialogVisible.value = false
    loadRules()
  } finally {
    submitting.value = false
  }
}

function resetForm() {
  formRef.value?.resetFields()
  form.rule_code = ''
  form.rule_name = ''
  form.rule_type = ''
  form.rule_content = ''
  form.language = '*'
  form.severity = '中'
}

onMounted(() => {
  loadRules()
})
</script>

<style scoped lang="scss">
.rule-config-page {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }
  }

  .rule-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 16px;
  }

  .rule-search {
    max-width: 420px;
  }

  .rule-count {
    color: #909399;
    font-size: 13px;
    white-space: nowrap;
  }
}
</style>
