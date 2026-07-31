<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, Delete, Plus, Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  bindStudioSkill,
  createStudioAgent,
  createStudioSkill,
  getAgentVersion,
  listAgentVersions,
  listStudioAgents,
  listStudioSkills,
  submitStudioAgent,
  testStudioAgent,
  unbindStudioSkill,
  withdrawStudioAgent,
} from '@/api/agentStudio'
import type { AgentVersionDetail, SkillType, StudioAsset } from '@/types/agentStudio'

const steps = ['基本信息', '审查职责', '系统提示词', 'Skill', '能力权限', '测试', '提交审批']
const activeStep = ref(0)
const loading = ref(false)
const agents = ref<StudioAsset[]>([])
const skills = ref<StudioAsset[]>([])
const currentAgent = ref<StudioAsset | null>(null)
const currentVersion = ref<AgentVersionDetail | null>(null)
const createdSkillVersionId = ref<number | null>(null)
const testPassed = computed(() => currentVersion.value?.status === 'testing')

const agentForm = reactive({
  code: '', name: '', description: '', review_focus: '', prompt: '', temperature: 0.2, max_tokens: 4096,
})
const skillForm = reactive({
  enabled: true,
  code: '',
  name: '',
  description: '',
  skill_type: 'llm_transform' as SkillType,
  prompt: '',
  tool_code: 'detect_language',
  arguments_json: '{}',
  agent_code: '',
  workflow_ids: '',
})

const skillTypeOptions = [
  { label: '模型转换', value: 'llm_transform' },
  { label: '只读工具', value: 'readonly_tool' },
  { label: 'Agent 委派', value: 'agent_delegate' },
  { label: '顺序工作流', value: 'sequence_workflow' },
]
const readonlyTools = [
  'analyze_project', 'dashboard_summary', 'detect_language', 'list_agents', 'list_code_files',
  'list_projects', 'list_reports', 'list_review_issues', 'list_review_tasks', 'list_rules',
]

async function loadAssets(): Promise<void> {
  loading.value = true
  try {
    ;[agents.value, skills.value] = await Promise.all([listStudioAgents(), listStudioSkills()])
  } finally {
    loading.value = false
  }
}

function validateStep(): boolean {
  if (activeStep.value === 0 && (!agentForm.code || !agentForm.name)) return false
  if (activeStep.value === 1 && agentForm.review_focus.trim().length < 2) return false
  if (activeStep.value === 2 && agentForm.prompt.trim().length < 20) return false
  if (activeStep.value === 3 && skillForm.enabled && (!skillForm.code || !skillForm.name)) return false
  return true
}

function nextStep(): void {
  if (!validateStep()) {
    ElMessage.warning('请完整填写当前步骤')
    return
  }
  activeStep.value = Math.min(steps.length - 1, activeStep.value + 1)
}

function skillDefinition(): Record<string, unknown> {
  if (skillForm.skill_type === 'llm_transform') return { prompt: skillForm.prompt }
  if (skillForm.skill_type === 'readonly_tool') {
    let args: Record<string, unknown>
    try { args = JSON.parse(skillForm.arguments_json) as Record<string, unknown> } catch { throw new Error('工具参数必须是 JSON 对象') }
    return { tool_code: skillForm.tool_code, arguments: args }
  }
  if (skillForm.skill_type === 'agent_delegate') return { agent_code: skillForm.agent_code, max_depth: 2 }
  const ids = skillForm.workflow_ids.split(',').map((item) => Number(item.trim())).filter(Boolean)
  return { steps: ids.map((skill_version_id) => ({ skill_version_id })) }
}

async function persistAndTest(): Promise<void> {
  loading.value = true
  try {
    if (!currentVersion.value) {
      const created = await createStudioAgent({
        code: agentForm.code,
        name: agentForm.name,
        description: agentForm.description,
        prompt: agentForm.prompt,
        review_focus: agentForm.review_focus,
        model_config_json: { temperature: agentForm.temperature, max_tokens: agentForm.max_tokens },
      })
      currentAgent.value = created.agent
      currentVersion.value = await getAgentVersion(created.version.id)
    }
    if (skillForm.enabled && !createdSkillVersionId.value) {
      const skill = await createStudioSkill({
        code: skillForm.code,
        name: skillForm.name,
        description: skillForm.description,
        skill_type: skillForm.skill_type,
        definition: skillDefinition(),
        requested_capabilities: skillForm.skill_type === 'readonly_tool' ? ['readonly_tool'] : [],
      })
      createdSkillVersionId.value = skill.version.id
      await bindStudioSkill(currentVersion.value.id, {
        skill_version_id: skill.version.id,
        position: currentVersion.value.bindings.length,
        config: {},
      })
    }
    await testStudioAgent(currentVersion.value.id)
    currentVersion.value = await getAgentVersion(currentVersion.value.id)
    ElMessage.success('版本测试通过')
    await loadAssets()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '测试失败')
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!currentVersion.value || !testPassed.value) return
  const { value } = await ElMessageBox.prompt('填写提交说明', '提交发布审批', {
    inputPlaceholder: '变更目标与风险说明',
    confirmButtonText: '提交',
    cancelButtonText: '取消',
  })
  await submitStudioAgent(currentVersion.value.id, value)
  currentVersion.value = await getAgentVersion(currentVersion.value.id)
  ElMessage.success('已提交管理员审批')
  await loadAssets()
}

async function resume(agent: StudioAsset): Promise<void> {
  const versions = await listAgentVersions(agent.id)
  if (!versions.length) return
  const detail = await getAgentVersion(versions[0].id)
  currentAgent.value = agent
  currentVersion.value = detail
  Object.assign(agentForm, {
    code: agent.code,
    name: agent.name,
    description: agent.description || '',
    review_focus: detail.review_focus,
    prompt: detail.prompt,
    temperature: detail.model_config.temperature ?? 0.2,
    max_tokens: detail.model_config.max_tokens ?? 4096,
  })
  activeStep.value = detail.status === 'pending_approval' ? 6 : 5
}

async function removeBinding(bindingId: number): Promise<void> {
  await unbindStudioSkill(bindingId)
  if (currentVersion.value) currentVersion.value = await getAgentVersion(currentVersion.value.id)
}

async function withdraw(): Promise<void> {
  if (!currentVersion.value) return
  await withdrawStudioAgent(currentVersion.value.id, '审查员撤回修订')
  currentVersion.value = await getAgentVersion(currentVersion.value.id)
  ElMessage.success('已撤回审批')
}

function resetWizard(): void {
  currentAgent.value = null
  currentVersion.value = null
  createdSkillVersionId.value = null
  Object.assign(agentForm, { code: '', name: '', description: '', review_focus: '', prompt: '', temperature: 0.2, max_tokens: 4096 })
  Object.assign(skillForm, { enabled: true, code: '', name: '', description: '', skill_type: 'llm_transform', prompt: '', tool_code: 'detect_language', arguments_json: '{}', agent_code: '', workflow_ids: '' })
  activeStep.value = 0
}

onMounted(loadAssets)
</script>

<template>
  <div class="studio-page prism-page-shell">
    <header class="studio-head prism-page-head">
      <div>
        <h2 class="font-display">Agent 工坊</h2>
        <div class="status-line">{{ currentAgent ? `${currentAgent.name} · v${currentVersion?.version_number || 1}` : '新建审查 Agent' }}</div>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadAssets">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="resetWizard">新建</el-button>
      </div>
    </header>

    <section class="asset-strip">
      <button v-for="item in agents" :key="item.id" type="button" class="asset-item" @click="resume(item)">
        <span><b>{{ item.name }}</b><code>{{ item.code }}</code></span>
        <el-tag size="small" effect="plain">{{ item.status }}</el-tag>
      </button>
      <span v-if="!agents.length" class="empty-inline">暂无自定义 Agent</span>
    </section>

    <section class="workflow-shell" v-loading="loading">
      <el-steps :active="activeStep" finish-status="success" align-center>
        <el-step v-for="step in steps" :key="step" :title="step" />
      </el-steps>

      <div class="step-body">
        <el-form v-if="activeStep === 0" :model="agentForm" label-position="top">
          <div class="form-grid three">
            <el-form-item label="Agent 编码"><el-input v-model="agentForm.code" :disabled="!!currentAgent" placeholder="reliability_reviewer" /></el-form-item>
            <el-form-item label="名称"><el-input v-model="agentForm.name" maxlength="120" /></el-form-item>
            <el-form-item label="说明"><el-input v-model="agentForm.description" maxlength="500" /></el-form-item>
          </div>
        </el-form>

        <el-form v-else-if="activeStep === 1" :model="agentForm" label-position="top">
          <el-form-item label="审查重点"><el-input v-model="agentForm.review_focus" type="textarea" :rows="8" maxlength="4000" show-word-limit /></el-form-item>
        </el-form>

        <el-form v-else-if="activeStep === 2" :model="agentForm" label-position="top">
          <el-form-item label="系统提示词"><el-input v-model="agentForm.prompt" type="textarea" :rows="12" maxlength="30000" show-word-limit /></el-form-item>
          <div class="form-grid two compact">
            <el-form-item label="Temperature"><el-slider v-model="agentForm.temperature" :min="0" :max="1" :step="0.1" show-input /></el-form-item>
            <el-form-item label="最大输出 Token"><el-input-number v-model="agentForm.max_tokens" :min="128" :max="4096" :step="128" /></el-form-item>
          </div>
        </el-form>

        <el-form v-else-if="activeStep === 3" :model="skillForm" label-position="top">
          <el-form-item><el-switch v-model="skillForm.enabled" active-text="绑定专属 Skill" inactive-text="无 Skill" /></el-form-item>
          <template v-if="skillForm.enabled">
            <div class="form-grid three">
              <el-form-item label="Skill 编码"><el-input v-model="skillForm.code" placeholder="normalize_findings" /></el-form-item>
              <el-form-item label="名称"><el-input v-model="skillForm.name" /></el-form-item>
              <el-form-item label="类型"><el-select v-model="skillForm.skill_type"><el-option v-for="item in skillTypeOptions" :key="item.value" v-bind="item" /></el-select></el-form-item>
            </div>
            <el-form-item label="说明"><el-input v-model="skillForm.description" /></el-form-item>
            <el-form-item v-if="skillForm.skill_type === 'llm_transform'" label="转换提示词"><el-input v-model="skillForm.prompt" type="textarea" :rows="6" /></el-form-item>
            <template v-else-if="skillForm.skill_type === 'readonly_tool'">
              <el-form-item label="只读工具"><el-select v-model="skillForm.tool_code" filterable><el-option v-for="tool in readonlyTools" :key="tool" :label="tool" :value="tool" /></el-select></el-form-item>
              <el-form-item label="固定参数 JSON"><el-input v-model="skillForm.arguments_json" type="textarea" :rows="5" class="mono-input" /></el-form-item>
            </template>
            <el-form-item v-else-if="skillForm.skill_type === 'agent_delegate'" label="已发布 Agent 编码"><el-input v-model="skillForm.agent_code" /></el-form-item>
            <el-form-item v-else label="Skill 版本 ID（逗号分隔）"><el-input v-model="skillForm.workflow_ids" /></el-form-item>
          </template>
        </el-form>

        <div v-else-if="activeStep === 4" class="permission-grid">
          <div><span>网络请求</span><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div><span>Shell / 子进程</span><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div><span>文件与数据库写入</span><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div><span>只读工具</span><el-tag :type="skillForm.skill_type === 'readonly_tool' ? 'success' : 'info'" effect="plain">{{ skillForm.skill_type === 'readonly_tool' ? skillForm.tool_code : '未申请' }}</el-tag></div>
        </div>

        <div v-else-if="activeStep === 5" class="test-panel">
          <el-result :icon="testPassed ? 'success' : 'info'" :title="testPassed ? '测试已通过' : '等待测试'" :sub-title="currentVersion ? `checksum ${currentVersion.checksum.slice(0, 16)}…` : '版本尚未落库'">
            <template #extra><el-button type="primary" :icon="Check" :loading="loading" @click="persistAndTest">执行测试</el-button></template>
          </el-result>
          <div v-if="currentVersion?.bindings.length" class="binding-list">
            <div v-for="item in currentVersion.bindings" :key="item.id"><code>Skill vID {{ item.skill_version_id }}</code><el-button text type="danger" :icon="Delete" @click="removeBinding(item.id)" /></div>
          </div>
        </div>

        <div v-else class="submit-panel">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="Agent">{{ agentForm.name }}</el-descriptions-item>
            <el-descriptions-item label="版本">v{{ currentVersion?.version_number || '-' }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ currentVersion?.status || '未测试' }}</el-descriptions-item>
            <el-descriptions-item label="Skill">{{ currentVersion?.bindings.length || 0 }}</el-descriptions-item>
          </el-descriptions>
          <div class="submit-actions">
            <el-button v-if="currentVersion?.status === 'pending_approval'" @click="withdraw">撤回</el-button>
            <el-button type="primary" :icon="Upload" :disabled="!testPassed" @click="submit">提交审批</el-button>
          </div>
        </div>
      </div>

      <footer class="step-footer">
        <el-button :disabled="activeStep === 0" @click="activeStep--">上一步</el-button>
        <el-button v-if="activeStep < steps.length - 1" type="primary" @click="nextStep">下一步</el-button>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
.studio-page { display: grid; gap: 18px; }
.studio-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.studio-head h2 { margin: 0; font-size: 24px; }
.status-line { margin-top: 6px; color: var(--gray-500); }
.head-actions { display: flex; gap: 8px; }
.asset-strip { display: flex; gap: 8px; padding-bottom: 4px; overflow-x: auto; }
.asset-item { min-width: 210px; height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 12px; border: 1px solid var(--gray-200); border-radius: 6px; background: #fff; cursor: pointer; text-align: left; }
.asset-item:hover { border-color: var(--brand-400); }
.asset-item span { display: grid; min-width: 0; }
.asset-item b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-item code { color: var(--gray-500); font-size: 11px; }
.empty-inline { color: var(--gray-500); padding: 16px 0; }
.workflow-shell { background: #fff; border-top: 1px solid var(--gray-200); padding: 24px 0 0; }
.step-body { min-height: 360px; padding: 34px max(20px, 7vw) 20px; }
.form-grid { display: grid; gap: 16px; }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.form-grid.compact { align-items: end; }
.permission-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: 1px solid var(--gray-200); }
.permission-grid > div { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px; border-bottom: 1px solid var(--gray-200); }
.permission-grid > div:nth-child(odd) { border-right: 1px solid var(--gray-200); }
.binding-list { border-top: 1px solid var(--gray-200); }
.binding-list > div { display: flex; align-items: center; justify-content: space-between; min-height: 44px; }
.submit-panel { display: grid; gap: 24px; }
.submit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.step-footer { display: flex; justify-content: space-between; padding: 16px 88px 16px 0; border-top: 1px solid var(--gray-200); }
.mono-input :deep(textarea) { font-family: var(--font-mono); }
@media (max-width: 820px) {
  .studio-head { flex-direction: column; }
  .form-grid.three, .form-grid.two, .permission-grid { grid-template-columns: 1fr; }
  .permission-grid > div:nth-child(odd) { border-right: 0; }
  .step-body { padding: 26px 0 18px; }
  .workflow-shell :deep(.el-step__title) { font-size: 11px; line-height: 1.2; overflow-wrap: anywhere; }
}
</style>
