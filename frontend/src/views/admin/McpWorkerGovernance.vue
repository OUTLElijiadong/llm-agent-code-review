<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Check,
  Connection,
  Delete,
  Edit,
  Link,
  Plus,
  Refresh,
  Search,
  Setting,
} from '@element-plus/icons-vue'

import {
  checkMcpServer,
  checkSandboxWorker,
  createCapabilityAlias,
  createMcpServer,
  createSandboxWorker,
  deleteCapabilityAlias,
  deleteMcpBinding,
  deleteMcpServer,
  listCapabilityAliases,
  listMcpBindings,
  listMcpServers,
  listMcpTools,
  listSandboxWorkers,
  seedProductionFallbackWorker,
  seedRecommendedMcpServers,
  syncMcpTools,
  updateCapabilityAlias,
  updateMcpServer,
  updateMcpTool,
  updateSandboxWorker,
  upsertMcpBinding,
} from '@/api/mcpGovernance'
import { listGovernanceAgents } from '@/api/adminGovernance'
import type {
  CapabilityAlias,
  McpBinding,
  McpServer,
  McpServerInput,
  McpTool,
  SandboxWorker,
  SandboxWorkerInput,
} from '@/types/mcpGovernance'
import type { GovernanceAgent } from '@/types/adminGovernance'
import type { SandboxLanguage, SandboxTestMode } from '@/types/sandbox'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

type TabName = 'servers' | 'tools' | 'bindings' | 'aliases' | 'workers'

const activeTab = ref<TabName>('servers')
const loading = ref(false)
const acting = ref(false)
const servers = ref<McpServer[]>([])
const tools = ref<McpTool[]>([])
const bindings = ref<McpBinding[]>([])
const aliases = ref<CapabilityAlias[]>([])
const workers = ref<SandboxWorker[]>([])
const agents = ref<GovernanceAgent[]>([])
const serverFilter = ref(0)
const agentFilter = ref('')
const aliasFilter = ref('')
const serverDialog = ref(false)
const bindingDialog = ref(false)
const aliasDialog = ref(false)
const workerDialog = ref(false)
const editingServerId = ref(0)
const editingAliasId = ref(0)
const editingWorkerId = ref(0)

const serverForm = reactive<McpServerInput & { headers_text: string }>({
  code: '', name: '', description: '', transport: 'streamable_http', url: '', auth_type: 'none',
  managed_kind: null, enabled: false, credential_required: false, headers_text: '',
})
const bindingForm = reactive({
  agent_code: '', tool_id: null as number | null, permission: 'allow' as McpBinding['permission'],
  requires_approval: true, enabled: true,
})
const aliasForm = reactive({
  capability_code: '', alias: '', locale: 'zh-CN', weight: 1, enabled: true,
})
const workerForm = reactive<SandboxWorkerInput>({
  code: '', name: '', worker_type: 'managed', transport: 'https', endpoint: '', token: '',
  supported_languages: ['python', 'node', 'java', 'go', 'php'],
  supported_modes: ['whitebox', 'blackbox', 'combined', 'deploy'], runtime: 'runsc',
  max_concurrency: 1, priority: 50, enabled: false,
})

const filteredTools = computed(() => serverFilter.value
  ? tools.value.filter((tool) => tool.server_id === serverFilter.value)
  : tools.value)
const filteredBindings = computed(() => agentFilter.value
  ? bindings.value.filter((binding) => binding.agent_code === agentFilter.value)
  : bindings.value)
const filteredAliases = computed(() => {
  const query = aliasFilter.value.trim().toLowerCase()
  if (!query) return aliases.value
  return aliases.value.filter((item) => `${item.capability_code} ${item.alias}`.toLowerCase().includes(query))
})
const enabledTools = computed(() => tools.value.filter((tool) => tool.enabled && tool.server_status === 'healthy'))

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'healthy') return 'success'
  if (status === 'unhealthy' || status === 'blocked') return 'danger'
  if (status === 'unknown' || status === 'registered' || status === 'credential_required') return 'warning'
  return 'info'
}

function formatTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const settled = await Promise.allSettled([
      listMcpServers(), listMcpTools(), listMcpBindings(), listCapabilityAliases(),
      listSandboxWorkers(), listGovernanceAgents(),
    ])
    if (settled[0].status === 'fulfilled') servers.value = settled[0].value
    if (settled[1].status === 'fulfilled') tools.value = settled[1].value
    if (settled[2].status === 'fulfilled') bindings.value = settled[2].value
    if (settled[3].status === 'fulfilled') aliases.value = settled[3].value
    if (settled[4].status === 'fulfilled') workers.value = settled[4].value
    if (settled[5].status === 'fulfilled') agents.value = settled[5].value
  } finally { loading.value = false }
}

function resetServerForm(): void {
  editingServerId.value = 0
  Object.assign(serverForm, {
    code: '', name: '', description: '', transport: 'streamable_http', url: '', auth_type: 'none',
    managed_kind: null, enabled: false, credential_required: false, headers_text: '',
  })
}

function openServerDialog(server?: McpServer): void {
  resetServerForm()
  if (server) {
    editingServerId.value = server.id
    Object.assign(serverForm, {
      code: server.code, name: server.name, description: server.description, transport: server.transport,
      url: server.url, auth_type: server.auth_type, managed_kind: server.managed_kind,
      enabled: server.enabled, credential_required: server.credential_required, headers_text: '',
    })
  }
  serverDialog.value = true
}

async function saveServer(): Promise<void> {
  if (!serverForm.code.trim() || !serverForm.name.trim()) {
    ElMessage.warning('请填写 Server 编码和名称')
    return
  }
  if (serverForm.transport === 'streamable_http' && !serverForm.url.trim()) {
    ElMessage.warning('远程 MCP 必须填写 HTTPS 地址')
    return
  }
  let headers: Record<string, string> | undefined
  if (serverForm.headers_text.trim()) {
    try {
      const parsed = JSON.parse(serverForm.headers_text)
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not object')
      headers = Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]))
    } catch {
      ElMessage.warning('请求头必须是 JSON 对象')
      return
    }
  }
  const payload: McpServerInput = {
    code: serverForm.code.trim(), name: serverForm.name.trim(), description: serverForm.description.trim(),
    transport: serverForm.transport, url: serverForm.transport === 'managed' ? '' : serverForm.url.trim(),
    auth_type: serverForm.auth_type, managed_kind: serverForm.transport === 'managed' ? serverForm.managed_kind : null,
    enabled: serverForm.enabled, credential_required: serverForm.credential_required,
    ...(headers ? { headers } : {}),
  }
  acting.value = true
  try {
    if (editingServerId.value) await updateMcpServer(editingServerId.value, payload)
    else await createMcpServer(payload)
    serverDialog.value = false
    await loadAll()
    ElMessage.success('MCP Server 已保存')
  } finally { acting.value = false }
}

async function removeServer(server: McpServer): Promise<void> {
  await ElMessageBox.confirm(`删除 ${server.name} 会同时删除工具与 Agent 绑定，是否继续？`, '删除 MCP Server', { type: 'warning' })
  await deleteMcpServer(server.id)
  await loadAll()
  ElMessage.success('MCP Server 已删除')
}

async function seedRecommended(): Promise<void> {
  acting.value = true
  try {
    await seedRecommendedMcpServers()
    await loadAll()
    ElMessage.success('推荐 MCP 已登记；需凭据或执行器的服务仍保持禁用')
  } finally { acting.value = false }
}

async function runServerAction(server: McpServer, action: 'health' | 'sync'): Promise<void> {
  acting.value = true
  try {
    if (action === 'health') await checkMcpServer(server.id)
    else await syncMcpTools(server.id)
    await loadAll()
    ElMessage.success(action === 'health' ? '健康检查完成' : '工具 Schema 已同步')
  } finally { acting.value = false }
}

async function toggleTool(tool: McpTool, enabled: boolean): Promise<void> {
  try {
    const updated = await updateMcpTool(tool.id, { enabled })
    Object.assign(tool, updated)
  } catch {
    tool.enabled = !enabled
  }
}

async function changeToolRisk(tool: McpTool, risk: McpTool['risk_level']): Promise<void> {
  const updated = await updateMcpTool(tool.id, { risk_level: risk })
  Object.assign(tool, updated)
  ElMessage.success('工具风险等级已更新')
}

function openBindingDialog(binding?: McpBinding): void {
  Object.assign(bindingForm, {
    agent_code: binding?.agent_code || '', tool_id: binding?.tool_id || null,
    permission: binding?.permission || 'allow', requires_approval: binding?.requires_approval ?? true,
    enabled: binding?.enabled ?? true,
  })
  bindingDialog.value = true
}

async function saveBinding(): Promise<void> {
  if (!bindingForm.agent_code || !bindingForm.tool_id) {
    ElMessage.warning('请选择 Agent 和已启用工具')
    return
  }
  acting.value = true
  try {
    await upsertMcpBinding({ ...bindingForm, tool_id: bindingForm.tool_id })
    bindingDialog.value = false
    bindings.value = await listMcpBindings()
    ElMessage.success('Agent 工具绑定已保存')
  } finally { acting.value = false }
}

async function removeBinding(binding: McpBinding): Promise<void> {
  await deleteMcpBinding(binding.id)
  bindings.value = await listMcpBindings()
  ElMessage.success('绑定已删除')
}

function openAliasDialog(alias?: CapabilityAlias): void {
  editingAliasId.value = alias?.id || 0
  Object.assign(aliasForm, {
    capability_code: alias?.capability_code || '', alias: alias?.alias || '', locale: alias?.locale || 'zh-CN',
    weight: alias?.weight ?? 1, enabled: alias?.enabled ?? true,
  })
  aliasDialog.value = true
}

async function saveAlias(): Promise<void> {
  if (!aliasForm.capability_code.trim() || !aliasForm.alias.trim()) {
    ElMessage.warning('请填写能力编码与近义词')
    return
  }
  acting.value = true
  try {
    const payload = { ...aliasForm, capability_code: aliasForm.capability_code.trim(), alias: aliasForm.alias.trim() }
    if (editingAliasId.value) await updateCapabilityAlias(editingAliasId.value, payload)
    else await createCapabilityAlias(payload)
    aliasDialog.value = false
    aliases.value = await listCapabilityAliases()
    ElMessage.success('能力别名已保存')
  } finally { acting.value = false }
}

async function removeAlias(alias: CapabilityAlias): Promise<void> {
  await deleteCapabilityAlias(alias.id)
  aliases.value = await listCapabilityAliases()
  ElMessage.success('能力别名已删除')
}

function resetWorkerForm(): void {
  editingWorkerId.value = 0
  Object.assign(workerForm, {
    code: '', name: '', worker_type: 'managed', transport: 'https', endpoint: '', token: '',
    supported_languages: ['python', 'node', 'java', 'go', 'php'],
    supported_modes: ['whitebox', 'blackbox', 'combined', 'deploy'], runtime: 'runsc',
    max_concurrency: 1, priority: 50, enabled: false,
  })
}

function openWorkerDialog(worker?: SandboxWorker): void {
  resetWorkerForm()
  if (worker) {
    editingWorkerId.value = worker.id
    Object.assign(workerForm, {
      code: worker.code, name: worker.name, worker_type: worker.worker_type, transport: worker.transport,
      endpoint: worker.endpoint, token: '', supported_languages: [...worker.supported_languages],
      supported_modes: [...worker.supported_modes], runtime: worker.runtime,
      max_concurrency: worker.max_concurrency, priority: worker.priority, enabled: worker.enabled,
    })
  }
  workerDialog.value = true
}

async function saveWorker(): Promise<void> {
  if (!workerForm.code.trim() || !workerForm.name.trim() || !workerForm.endpoint.trim()) {
    ElMessage.warning('请填写 worker 编码、名称和端点')
    return
  }
  if (!workerForm.supported_languages.length || !workerForm.supported_modes.length) {
    ElMessage.warning('至少选择一种语言和一种模式')
    return
  }
  acting.value = true
  try {
    const payload = { ...workerForm, code: workerForm.code.trim(), name: workerForm.name.trim(), endpoint: workerForm.endpoint.trim() }
    if (editingWorkerId.value) await updateSandboxWorker(editingWorkerId.value, payload)
    else await createSandboxWorker(payload)
    workerDialog.value = false
    workers.value = await listSandboxWorkers()
    ElMessage.success('worker 已保存；启用后仍需健康检查通过才会接收任务')
  } finally { acting.value = false }
}

async function runWorkerHealth(worker: SandboxWorker): Promise<void> {
  acting.value = true
  try {
    await checkSandboxWorker(worker.id)
    workers.value = await listSandboxWorkers()
    ElMessage.success('worker 健康检查完成')
  } finally { acting.value = false }
}

async function seedProductionWorker(): Promise<void> {
  acting.value = true
  try {
    await seedProductionFallbackWorker()
    workers.value = await listSandboxWorkers()
    ElMessage.success('生产受限兜底 worker 已登记')
  } finally { acting.value = false }
}

onMounted(loadAll)
</script>

<template>
  <div class="governance-page" v-loading="loading">
    <div class="governance-heading">
      <div>
        <div class="eyebrow font-mono">SUPER ADMIN ONLY</div>
        <h2>MCP 与沙箱节点</h2>
        <p>治理外部工具发现、Agent 能力绑定、近义词补齐与隔离 worker 调度。</p>
      </div>
      <el-button :icon="Refresh" @click="loadAll">刷新全量状态</el-button>
    </div>

    <el-tabs v-model="activeTab" class="governance-tabs">
      <el-tab-pane name="servers" label="MCP Server">
        <div class="toolbar">
          <div class="metric-line"><b>{{ servers.length }}</b> 个服务 <span>·</span> <b>{{ servers.filter((item) => item.status === 'healthy').length }}</b> 健康</div>
          <div><el-button :icon="Connection" :loading="acting" @click="seedRecommended">登记推荐服务</el-button><el-button type="primary" :icon="Plus" @click="openServerDialog()">新增远程 MCP</el-button></div>
        </div>
        <el-table :data="servers" stripe>
          <el-table-column label="服务" min-width="210">
            <template #default="{ row }"><div class="primary-cell"><b>{{ row.name }}</b><small>{{ row.code }} · {{ row.transport }}</small></div></template>
          </el-table-column>
          <el-table-column prop="url" label="端点" min-width="250" show-overflow-tooltip>
            <template #default="{ row }">{{ row.url || `managed://${row.managed_kind || row.code}` }}</template>
          </el-table-column>
          <el-table-column label="状态" width="145"><template #default="{ row }"><el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
          <el-table-column label="凭据/工具" width="130"><template #default="{ row }">{{ row.has_credentials ? '已配置' : row.credential_required ? '待配置' : '无需' }} / {{ row.tool_count }}</template></el-table-column>
          <el-table-column label="操作" width="270" fixed="right">
            <template #default="{ row }">
              <el-button link :icon="Check" :loading="acting" @click="runServerAction(row, 'health')">检查</el-button>
              <el-button link :icon="Refresh" :loading="acting" @click="runServerAction(row, 'sync')">同步</el-button>
              <el-button link :icon="Edit" @click="openServerDialog(row)">编辑</el-button>
              <el-button link type="danger" :icon="Delete" @click="removeServer(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="tools" label="工具 Schema">
        <div class="toolbar">
          <el-select v-model="serverFilter" style="width: 220px"><el-option label="全部 MCP Server" :value="0" /><el-option v-for="server in servers" :key="server.id" :label="server.name" :value="server.id" /></el-select>
          <div class="metric-line">Schema 变化会自动停用工具与绑定</div>
        </div>
        <el-table :data="filteredTools" stripe>
          <el-table-column label="工具" min-width="260"><template #default="{ row }"><div class="primary-cell"><b>{{ row.display_name }}</b><small>{{ row.model_name }}</small></div></template></el-table-column>
          <el-table-column prop="server_code" label="Server" width="130" />
          <el-table-column prop="description" label="能力说明" min-width="260" show-overflow-tooltip />
          <el-table-column label="风险" width="130"><template #default="{ row }"><el-select :model-value="row.risk_level" size="small" @change="changeToolRisk(row, $event)"><el-option label="低" value="low" /><el-option label="中" value="medium" /><el-option label="高" value="high" /><el-option label="关键" value="critical" /></el-select></template></el-table-column>
          <el-table-column label="启用" width="90"><template #default="{ row }"><el-switch v-model="row.enabled" @change="toggleTool(row, Boolean($event))" /></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="bindings" label="Agent 绑定">
        <div class="toolbar">
          <el-select v-model="agentFilter" clearable placeholder="全部 Agent" style="width: 220px"><el-option v-for="agent in agents" :key="agent.code" :label="`${agent.name} · ${agent.code}`" :value="agent.code" /></el-select>
          <el-button type="primary" :icon="Link" @click="openBindingDialog()">绑定工具</el-button>
        </div>
        <el-table :data="filteredBindings" stripe>
          <el-table-column prop="agent_code" label="Agent" min-width="170" />
          <el-table-column label="工具" min-width="260"><template #default="{ row }"><div class="primary-cell"><b>{{ row.tool_code }}</b><small>{{ row.server_code }}</small></div></template></el-table-column>
          <el-table-column prop="permission" label="策略" width="110" />
          <el-table-column label="审批" width="100"><template #default="{ row }">{{ row.requires_approval ? '需要' : '无需' }}</template></el-table-column>
          <el-table-column label="Schema" width="100"><template #default="{ row }"><el-tag size="small" :type="row.schema_current ? 'success' : 'danger'">{{ row.schema_current ? '一致' : '已漂移' }}</el-tag></template></el-table-column>
          <el-table-column label="操作" width="150"><template #default="{ row }"><el-button link :icon="Edit" @click="openBindingDialog(row)">编辑</el-button><el-button link type="danger" :icon="Delete" @click="removeBinding(row)">删除</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="aliases" label="近义词补齐">
        <div class="toolbar">
          <el-input v-model="aliasFilter" :prefix-icon="Search" clearable placeholder="搜索能力编码或近义词" style="width: min(360px, 100%)" />
          <el-button type="primary" :icon="Plus" @click="openAliasDialog()">新增近义词</el-button>
        </div>
        <el-table :data="filteredAliases" stripe>
          <el-table-column prop="capability_code" label="能力编码" min-width="260" />
          <el-table-column prop="alias" label="近义词" min-width="180" />
          <el-table-column prop="locale" label="语言" width="100" />
          <el-table-column prop="weight" label="权重" width="90" />
          <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
          <el-table-column label="操作" width="150"><template #default="{ row }"><el-button link :icon="Edit" @click="openAliasDialog(row)">编辑</el-button><el-button link type="danger" :icon="Delete" @click="removeAlias(row)">删除</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="workers" label="Sandbox Worker">
        <div class="toolbar">
          <div class="metric-line"><b>{{ workers.filter((item) => item.status === 'healthy').length }}</b> / {{ workers.length }} 个节点健康</div>
          <div><el-button :icon="Setting" :loading="acting" @click="seedProductionWorker">登记生产兜底</el-button><el-button type="primary" :icon="Plus" @click="openWorkerDialog()">新增 worker</el-button></div>
        </div>
        <el-table :data="workers" stripe>
          <el-table-column label="节点" min-width="210"><template #default="{ row }"><div class="primary-cell"><b>{{ row.name }}</b><small>{{ row.code }} · {{ row.worker_type }}</small></div></template></el-table-column>
          <el-table-column prop="endpoint" label="端点" min-width="260" show-overflow-tooltip />
          <el-table-column label="运行时" width="120"><template #default="{ row }"><span class="font-mono">{{ row.runtime }}</span></template></el-table-column>
          <el-table-column label="能力" min-width="220"><template #default="{ row }">{{ row.supported_languages.join(' / ') }}<br><small>{{ row.supported_modes.join(' / ') }}</small></template></el-table-column>
          <el-table-column label="状态" width="135"><template #default="{ row }"><el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
          <el-table-column label="最后在线" width="150"><template #default="{ row }">{{ formatTime(row.last_seen_at) }}</template></el-table-column>
          <el-table-column label="操作" width="160" fixed="right"><template #default="{ row }"><el-button link :icon="Check" :loading="acting" @click="runWorkerHealth(row)">检查</el-button><el-button link :icon="Edit" @click="openWorkerDialog(row)">编辑</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="serverDialog" :title="editingServerId ? '编辑 MCP Server' : '新增 MCP Server'" width="min(620px, 94vw)">
      <el-form label-position="top">
        <div class="form-grid"><el-form-item label="编码"><el-input v-model="serverForm.code" :disabled="Boolean(editingServerId)" placeholder="github-enterprise" /></el-form-item><el-form-item label="名称"><el-input v-model="serverForm.name" /></el-form-item></div>
        <el-form-item label="说明"><el-input v-model="serverForm.description" type="textarea" :rows="2" /></el-form-item>
        <div class="form-grid"><el-form-item label="传输"><el-select v-model="serverForm.transport"><el-option label="Streamable HTTP" value="streamable_http" /><el-option label="平台受管" value="managed" /></el-select></el-form-item><el-form-item label="认证"><el-select v-model="serverForm.auth_type"><el-option label="无需认证" value="none" /><el-option label="Bearer" value="bearer" /><el-option label="自定义 Headers" value="headers" /><el-option label="OAuth 待配置" value="oauth_required" /></el-select></el-form-item></div>
        <el-form-item v-if="serverForm.transport === 'streamable_http'" label="HTTPS 地址"><el-input v-model="serverForm.url" placeholder="https://mcp.example/mcp" /></el-form-item>
        <el-form-item v-else label="受管类型"><el-select v-model="serverForm.managed_kind"><el-option label="Prism 源码" value="prism-code" /><el-option label="Prism 沙箱" value="prism-sandbox" /><el-option label="Playwright" value="playwright" /></el-select></el-form-item>
        <el-form-item label="加密请求头（JSON，留空不修改）"><el-input v-model="serverForm.headers_text" type="textarea" :rows="3" placeholder='{"Authorization":"Bearer ..."}' /></el-form-item>
        <div class="switch-line"><el-checkbox v-model="serverForm.credential_required">需要凭据</el-checkbox><el-switch v-model="serverForm.enabled" active-text="请求启用" /></div>
      </el-form>
      <template #footer><el-button @click="serverDialog = false">取消</el-button><el-button type="primary" :loading="acting" @click="saveServer">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="bindingDialog" title="Agent 工具绑定" width="min(520px, 94vw)">
      <el-form label-position="top"><el-form-item label="Agent"><el-select v-model="bindingForm.agent_code" filterable style="width: 100%"><el-option v-for="agent in agents" :key="agent.code" :label="`${agent.name} · ${agent.code}`" :value="agent.code" /></el-select></el-form-item><el-form-item label="健康且已启用的工具"><el-select v-model="bindingForm.tool_id" filterable style="width: 100%"><el-option v-for="tool in enabledTools" :key="tool.id" :label="`${tool.display_name} · ${tool.server_code}`" :value="tool.id" /></el-select></el-form-item><div class="form-grid"><el-form-item label="权限策略"><el-select v-model="bindingForm.permission"><el-option label="允许" value="allow" /><el-option label="拒绝" value="deny" /><el-option label="升级审批" value="escalate" /></el-select></el-form-item><el-form-item label="状态"><el-switch v-model="bindingForm.enabled" active-text="启用" /></el-form-item></div><el-checkbox v-model="bindingForm.requires_approval">工具调用前需要人工审批</el-checkbox></el-form>
      <template #footer><el-button @click="bindingDialog = false">取消</el-button><el-button type="primary" :loading="acting" @click="saveBinding">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="aliasDialog" :title="editingAliasId ? '编辑能力近义词' : '新增能力近义词'" width="min(520px, 94vw)">
      <el-form label-position="top"><el-form-item label="能力编码"><el-input v-model="aliasForm.capability_code" placeholder="agent:test_verifier 或 mcp:..." /></el-form-item><el-form-item label="近义词"><el-input v-model="aliasForm.alias" placeholder="如：整包源码扫描" /></el-form-item><div class="form-grid"><el-form-item label="语言"><el-input v-model="aliasForm.locale" /></el-form-item><el-form-item label="检索权重"><el-input-number v-model="aliasForm.weight" :min="0.1" :max="3" :step="0.1" /></el-form-item></div><el-switch v-model="aliasForm.enabled" active-text="启用" /></el-form>
      <template #footer><el-button @click="aliasDialog = false">取消</el-button><el-button type="primary" :loading="acting" @click="saveAlias">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="workerDialog" :title="editingWorkerId ? '编辑 Sandbox Worker' : '新增 Sandbox Worker'" width="min(680px, 94vw)">
      <el-form label-position="top"><div class="form-grid"><el-form-item label="编码"><el-input v-model="workerForm.code" :disabled="Boolean(editingWorkerId)" placeholder="managed-worker-01" /></el-form-item><el-form-item label="名称"><el-input v-model="workerForm.name" /></el-form-item></div><div class="form-grid"><el-form-item label="节点类型"><el-select v-model="workerForm.worker_type"><el-option label="本机节点" value="local" /><el-option label="托管节点" value="managed" /><el-option label="生产兜底" value="production_fallback" /></el-select></el-form-item><el-form-item label="传输"><el-select v-model="workerForm.transport"><el-option label="HTTPS" value="https" /><el-option label="Unix Socket" value="unix" /></el-select></el-form-item></div><el-form-item label="端点"><el-input v-model="workerForm.endpoint" :placeholder="workerForm.transport === 'https' ? 'https://worker.example' : '/run/prism-sandbox/executor.sock'" /></el-form-item><el-form-item label="认证 Token（留空不修改）"><el-input v-model="workerForm.token" type="password" show-password /></el-form-item><el-form-item label="支持语言"><el-checkbox-group v-model="workerForm.supported_languages"><el-checkbox v-for="value in (['python', 'node', 'java', 'go', 'php'] as SandboxLanguage[])" :key="value" :value="value">{{ value }}</el-checkbox></el-checkbox-group></el-form-item><el-form-item label="支持模式"><el-checkbox-group v-model="workerForm.supported_modes"><el-checkbox v-for="value in (['whitebox', 'blackbox', 'combined', 'deploy'] as SandboxTestMode[])" :key="value" :value="value">{{ value }}</el-checkbox></el-checkbox-group></el-form-item><div class="form-grid thirds"><el-form-item label="OCI 运行时"><el-input v-model="workerForm.runtime" /></el-form-item><el-form-item label="并发上限"><el-input-number v-model="workerForm.max_concurrency" :min="1" :max="8" /></el-form-item><el-form-item label="调度优先级"><el-input-number v-model="workerForm.priority" :min="0" :max="1000" /></el-form-item></div><el-switch v-model="workerForm.enabled" active-text="启用节点" /></el-form>
      <template #footer><el-button @click="workerDialog = false">取消</el-button><el-button type="primary" :loading="acting" @click="saveWorker">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.governance-page { min-width: 0; }
.governance-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 12px; }
.governance-heading h2 { margin: 3px 0 4px; font-size: 22px; letter-spacing: 0; }
.governance-heading p { margin: 0; color: var(--gray-500); font-size: 13px; }
.eyebrow { color: var(--color-primary); font-size: 10px; }
.governance-tabs { min-width: 0; padding: 0 16px 16px; border: var(--hairline); border-radius: 8px; background: var(--surface-1); box-shadow: var(--panel-shadow); }
.toolbar { min-height: 54px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.toolbar > div:last-child { display: flex; gap: 8px; }
.metric-line { color: var(--gray-500); font-size: 12px; }
.metric-line b { color: var(--gray-800); font-size: 16px; }
.metric-line span { margin: 0 6px; }
.primary-cell { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.primary-cell b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.primary-cell small, td small { color: var(--gray-500); font-size: 10px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.form-grid.thirds { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid :deep(.el-select), .form-grid :deep(.el-input-number) { width: 100%; }
.switch-line { display: flex; align-items: center; justify-content: space-between; gap: 12px; }

@media (max-width: 760px) {
  .governance-heading, .toolbar { align-items: stretch; flex-direction: column; }
  .toolbar { padding: 10px 0; }
  .toolbar > div:last-child { flex-wrap: wrap; }
  .form-grid, .form-grid.thirds { grid-template-columns: 1fr; gap: 0; }
  .governance-tabs { padding-inline: 10px; }
}
</style>
