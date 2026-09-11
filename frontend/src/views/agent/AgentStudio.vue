<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, Delete, MagicStick, Plus, Refresh, Upload } from '@element-plus/icons-vue'
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

const steps = [
  { title: '基本信息', hint: '编码与名称' },
  { title: '审查职责', hint: '划定关注点' },
  { title: '系统提示词', hint: '人格与规则' },
  { title: 'Skill', hint: '绑定能力' },
  { title: '能力权限', hint: '最小权限' },
  { title: '测试', hint: '沙箱试跑' },
  { title: '提交审批', hint: '管理员审核' },
]
const activeStep = ref(0)
const loading = ref(false)
const agents = ref<StudioAsset[]>([])
const skills = ref<StudioAsset[]>([])
const currentAgent = ref<StudioAsset | null>(null)
const currentVersion = ref<AgentVersionDetail | null>(null)
const createdSkillVersionId = ref<number | null>(null)
const testPassed = computed(() => currentVersion.value?.status === 'testing')

/* 状态中文标签(界面枚举必须中文,勿直接展示英文原值) */
const ASSET_STATUS_LABELS: Record<string, { label: string; tone: 'draft' | 'testing' | 'pending' | 'published' | 'rejected' }> = {
  draft: { label: '草稿', tone: 'draft' },
  testing: { label: '测试通过', tone: 'testing' },
  test_passed: { label: '测试通过', tone: 'testing' },
  pending_approval: { label: '待审批', tone: 'pending' },
  published: { label: '已发布', tone: 'published' },
  approved: { label: '已发布', tone: 'published' },
  rejected: { label: '已驳回', tone: 'rejected' },
  withdrawn: { label: '已撤回', tone: 'draft' },
}

function statusMeta(status?: string) {
  return ASSET_STATUS_LABELS[status ?? ''] ?? { label: status || '未知', tone: 'draft' as const }
}

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
    <header class="studio-hero prism-rise">
      <div class="hero-glow"></div>
      <div class="hero-body">
        <div class="hero-title">
          <span class="hero-icon"><el-icon><MagicStick /></el-icon></span>
          <div>
            <h2 class="font-display">Agent 工坊</h2>
            <p class="hero-sub">
              为审查流水线锻造专属 Agent —— 定义职责、编写提示词、绑定 Skill,测试通过后提交管理员审批发布。
            </p>
          </div>
        </div>
        <div class="hero-actions">
          <span v-if="currentAgent" class="hero-chip font-mono">
            {{ currentAgent.name }} · v{{ currentVersion?.version_number || 1 }}
          </span>
          <el-button :icon="Refresh" :loading="loading" round @click="loadAssets">刷新</el-button>
          <el-button type="primary" :icon="Plus" round @click="resetWizard">新建 Agent</el-button>
        </div>
      </div>
    </header>

    <section class="asset-grid prism-stagger" v-loading="loading">
      <button v-for="item in agents" :key="item.id" type="button" class="asset-card" :class="{ active: currentAgent?.id === item.id }" @click="resume(item)">
        <span class="asset-ico">🤖</span>
        <span class="asset-main">
          <b>{{ item.name }}</b>
          <code>{{ item.code }}</code>
        </span>
        <span class="status-chip" :data-tone="statusMeta(item.status).tone">{{ statusMeta(item.status).label }}</span>
      </button>
      <button v-if="!agents.length && !loading" type="button" class="asset-card empty" @click="resetWizard">
        <span class="asset-ico">✨</span>
        <span class="asset-main"><b>创建第一个审查 Agent</b><code>草稿仅自己可见,审批通过后全站生效</code></span>
      </button>
    </section>

    <section class="workflow-shell prism-rise" style="--rise-delay: 120ms" v-loading="loading">
      <nav class="steps-rail">
        <button
          v-for="(step, index) in steps"
          :key="step.title"
          type="button"
          class="step-node"
          :class="{ active: index === activeStep, done: index < activeStep }"
          :disabled="index > activeStep"
          @click="index < activeStep && (activeStep = index)"
        >
          <span class="step-no font-mono">{{ index < activeStep ? '✓' : index + 1 }}</span>
          <span class="step-txt"><b>{{ step.title }}</b><i>{{ step.hint }}</i></span>
        </button>
      </nav>

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
          <div class="perm-card denied"><span class="perm-ico">🚫</span><div><b>网络请求</b><i>草稿 Agent 一律禁止外联</i></div><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div class="perm-card denied"><span class="perm-ico">🚫</span><div><b>Shell / 子进程</b><i>不开放命令执行面</i></div><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div class="perm-card denied"><span class="perm-ico">🚫</span><div><b>文件与数据库写入</b><i>审查过程只读</i></div><el-tag type="danger" effect="plain">禁止</el-tag></div>
          <div class="perm-card" :class="{ allowed: skillForm.skill_type === 'readonly_tool' }">
            <span class="perm-ico">🛠️</span>
            <div><b>只读工具</b><i>白名单内可申请</i></div>
            <el-tag :type="skillForm.skill_type === 'readonly_tool' ? 'success' : 'info'" effect="plain">{{ skillForm.skill_type === 'readonly_tool' ? skillForm.tool_code : '未申请' }}</el-tag>
          </div>
        </div>

        <div v-else-if="activeStep === 5" class="test-panel">
          <div class="test-result-card" :class="{ passed: testPassed }">
            <span class="test-emoji">{{ testPassed ? '✅' : '🧪' }}</span>
            <div>
              <b>{{ testPassed ? '测试已通过' : '等待测试' }}</b>
              <p class="font-mono">{{ currentVersion ? `checksum ${currentVersion.checksum.slice(0, 16)}…` : '版本尚未落库' }}</p>
            </div>
            <el-button type="primary" :icon="Check" :loading="loading" round @click="persistAndTest">执行测试</el-button>
          </div>
          <div v-if="currentVersion?.bindings.length" class="binding-list">
            <div v-for="item in currentVersion.bindings" :key="item.id"><code>Skill vID {{ item.skill_version_id }}</code><el-button text type="danger" :icon="Delete" @click="removeBinding(item.id)" /></div>
          </div>
        </div>

        <div v-else class="submit-panel">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="Agent">{{ agentForm.name }}</el-descriptions-item>
            <el-descriptions-item label="版本">v{{ currentVersion?.version_number || '-' }}</el-descriptions-item>
            <el-descriptions-item label="状态">
              <span class="status-chip" :data-tone="statusMeta(currentVersion?.status).tone">{{ statusMeta(currentVersion?.status).label }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="Skill">{{ currentVersion?.bindings.length || 0 }}</el-descriptions-item>
          </el-descriptions>
          <div class="submit-actions">
            <el-button v-if="currentVersion?.status === 'pending_approval'" @click="withdraw">撤回</el-button>
            <el-button type="primary" :icon="Upload" :disabled="!testPassed" round @click="submit">提交审批</el-button>
          </div>
        </div>
      </div>

      <footer class="step-footer">
        <el-button :disabled="activeStep === 0" @click="activeStep--">上一步</el-button>
        <el-button v-if="activeStep < steps.length - 1" type="primary" round @click="nextStep">下一步</el-button>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
.studio-page { display: grid; gap: 20px; }

/* ── 头部英雄区 ── */
.studio-hero {
  position: relative; overflow: hidden;
  border-radius: 14px;
  background: linear-gradient(135deg, var(--brand-50, #eef4ff) 0%, #fff 55%);
  border: 1px solid var(--gray-200);
  padding: 22px 24px;
}
.hero-glow {
  position: absolute; inset: -40% -20% auto auto; width: 340px; height: 280px;
  background: radial-gradient(closest-side, rgba(64, 120, 244, .16), transparent);
  pointer-events: none;
}
.hero-body { position: relative; display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.hero-title { display: flex; gap: 14px; align-items: center; }
.hero-icon {
  width: 46px; height: 46px; border-radius: 12px; flex: none;
  display: grid; place-items: center; font-size: 22px; color: #fff;
  background: linear-gradient(135deg, var(--brand-500, #4078f4), var(--brand-600, #2f5ce0));
  box-shadow: 0 8px 20px rgba(64, 120, 244, .28);
}
.studio-hero h2 { margin: 0; font-size: 22px; }
.hero-sub { margin: 6px 0 0; max-width: 560px; color: var(--gray-500); font-size: 12.5px; line-height: 1.7; }
.hero-actions { display: flex; gap: 8px; align-items: center; flex: none; }
.hero-chip {
  padding: 5px 12px; border-radius: 999px; font-size: 11.5px;
  background: var(--brand-50, #eef4ff); color: var(--brand-600, #2f5ce0); border: 1px solid var(--brand-100, #d8e6ff);
}

/* ── Agent 卡片栅格 ── */
.asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.asset-card {
  display: flex; align-items: center; gap: 12px; padding: 14px 16px;
  border: 1px solid var(--gray-200); border-radius: 12px; background: #fff;
  cursor: pointer; text-align: left;
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
}
.asset-card:hover { transform: translateY(-2px); box-shadow: 0 10px 24px rgba(23, 34, 62, .08); border-color: var(--brand-300, #a8c4fa); }
.asset-card.active { border-color: var(--brand-500, #4078f4); box-shadow: 0 0 0 3px rgba(64, 120, 244, .14); }
.asset-ico { font-size: 22px; flex: none; }
.asset-main { display: grid; min-width: 0; flex: 1; }
.asset-main b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; }
.asset-main code { color: var(--gray-500); font-size: 11px; }
.asset-card.empty { border-style: dashed; color: var(--gray-500); }

/* ── 状态中文徽章 ── */
.status-chip {
  flex: none; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600;
}
.status-chip[data-tone='draft'] { background: var(--gray-100); color: var(--gray-600); }
.status-chip[data-tone='testing'] { background: rgba(64, 120, 244, .1); color: var(--brand-600, #2f5ce0); }
.status-chip[data-tone='pending'] { background: rgba(217, 168, 87, .14); color: #a87d2c; }
.status-chip[data-tone='published'] { background: rgba(64, 163, 99, .12); color: #2e8b57; }
.status-chip[data-tone='rejected'] { background: rgba(220, 73, 97, .1); color: var(--sev-severe, #dc4961); }

/* ── 步骤导航 ── */
.workflow-shell { background: #fff; border: 1px solid var(--gray-200); border-radius: 14px; overflow: hidden; }
.steps-rail { display: flex; gap: 4px; padding: 14px 18px 0; overflow-x: auto; border-bottom: 1px solid var(--gray-100); }
.step-node {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px 12px;
  border: none; background: none; cursor: pointer; text-align: left; position: relative;
  opacity: .62; transition: opacity .15s ease;
}
.step-node:disabled { cursor: default; }
.step-node.active, .step-node.done { opacity: 1; }
.step-node::after {
  content: ''; position: absolute; left: 10px; right: 10px; bottom: 0; height: 2.5px;
  border-radius: 2px; background: transparent; transition: background .2s ease;
}
.step-node.active::after { background: var(--brand-500, #4078f4); }
.step-node.done::after { background: var(--brand-200, #c9d9fb); }
.step-no {
  width: 22px; height: 22px; border-radius: 50%; flex: none; font-size: 11px;
  display: grid; place-items: center; color: var(--gray-500);
  background: var(--gray-100); transition: all .2s ease;
}
.step-node.done .step-no { background: rgba(64, 163, 99, .14); color: #2e8b57; }
.step-node.active .step-no { background: var(--brand-500, #4078f4); color: #fff; box-shadow: 0 4px 10px rgba(64, 120, 244, .32); }
.step-txt { display: grid; }
.step-txt b { font-size: 12.5px; color: var(--gray-800); }
.step-txt i { font-style: normal; font-size: 10.5px; color: var(--gray-400); }

.step-body { min-height: 360px; padding: 30px max(22px, 7vw) 22px; }
.form-grid { display: grid; gap: 16px; }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.form-grid.compact { align-items: end; }

/* ── 权限卡片 ── */
.permission-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.perm-card {
  display: flex; align-items: center; gap: 12px; padding: 16px;
  border: 1px solid var(--gray-200); border-radius: 12px; background: #fff;
  transition: border-color .18s ease, box-shadow .18s ease;
}
.perm-card.denied { background: linear-gradient(180deg, #fff, #fbfbfd); }
.perm-card.allowed { border-color: rgba(64, 163, 99, .4); box-shadow: 0 6px 16px rgba(64, 163, 99, .08); }
.perm-card > div { flex: 1; display: grid; }
.perm-card b { font-size: 13px; color: var(--gray-800); }
.perm-card i { font-style: normal; font-size: 11px; color: var(--gray-400); margin-top: 2px; }
.perm-ico { font-size: 20px; }

/* ── 测试面板 ── */
.test-panel { display: grid; gap: 14px; }
.test-result-card {
  display: flex; align-items: center; gap: 16px; padding: 22px 24px;
  border-radius: 12px; border: 1px dashed var(--gray-300);
  background: linear-gradient(180deg, #fff, #fafbff);
}
.test-result-card.passed { border-style: solid; border-color: rgba(64, 163, 99, .45); background: linear-gradient(180deg, #fff, #f4fbf7); }
.test-emoji { font-size: 30px; }
.test-result-card > div { flex: 1; }
.test-result-card b { font-size: 14.5px; }
.test-result-card p { margin: 4px 0 0; font-size: 11px; color: var(--gray-400); }
.binding-list { border-top: 1px solid var(--gray-100); }
.binding-list > div { display: flex; align-items: center; justify-content: space-between; min-height: 44px; }

.submit-panel { display: grid; gap: 24px; }
.submit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.step-footer { display: flex; justify-content: space-between; padding: 14px 22px; border-top: 1px solid var(--gray-100); background: #fbfcfe; }
.mono-input :deep(textarea) { font-family: var(--font-mono); }

@media (max-width: 820px) {
  .hero-body { flex-direction: column; }
  .form-grid.three, .form-grid.two, .permission-grid { grid-template-columns: 1fr; }
  .step-body { padding: 24px 16px 18px; }
  .step-node { padding: 8px 10px 10px; }
  .step-txt i { display: none; }
}
</style>
