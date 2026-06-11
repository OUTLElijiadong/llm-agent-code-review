<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Lock, RefreshRight, View } from '@element-plus/icons-vue'
import { scanAllProjects, scanFile, scanProject, scanTask } from '@/api/security'
import type {
  ApiEndpointOut,
  CodeLinkOut,
  DataFlowOut,
  SecurityFindingOut,
  SecurityScanOut,
} from '@/types/security'

interface Props {
  modelValue: boolean
  /** 'file' / 'task' / 'project' / 'all-projects' 四态 */
  source: 'file' | 'task' | 'project' | 'all-projects'
  /** 对应 id (file_id / task_id / project_id); all-projects 模式不需要 */
  refId: number | null
  /** project 模式可显示名,用于标题 */
  refName?: string
  /** 进入时是否自动触发扫描 */
  autoStart?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  refName: '',
  autoStart: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

// 扫描配置
const scanDepth = ref<'quick' | 'standard' | 'deep'>('standard')
const topN = ref(50)
const traceDataflow = ref(true)

// 状态
const loading = ref(false)
const result = ref<SecurityScanOut | null>(null)
const activeFindingIdx = ref(0)
const errorMessage = ref('')

const dialogTitle = computed(() => {
  const map = {
    file: '文件安全扫描',
    task: '任务安全复审',
    project: '项目威胁建模',
    'all-projects': '全量项目安全扫描',
  }
  return `🛡 ${map[props.source]}`
})

const isProjectScan = computed(() => {
  return props.source === 'project' || props.source === 'all-projects'
})

const scopeLabel = computed(() => {
  if (props.source === 'all-projects') return '全部可见项目'
  return `${props.source}#${props.refId ?? '-'}`
})

const severityCounts = computed(() => {
  const counts: Record<string, number> = { 严重: 0, 高: 0, 中: 0, 低: 0 }
  if (!result.value) return counts
  for (const f of result.value.findings) {
    if (f.severity in counts) counts[f.severity]++
  }
  return counts
})

const sortedFindings = computed<SecurityFindingOut[]>(() => {
  if (!result.value) return []
  const order: Record<string, number> = { 严重: 0, 高: 1, 中: 2, 低: 3 }
  return [...result.value.findings].sort((a, b) => {
    return (order[a.severity] ?? 9) - (order[b.severity] ?? 9)
  })
})

const groupedByOwasp = computed(() => {
  const map = new Map<string, SecurityFindingOut[]>()
  for (const f of sortedFindings.value) {
    const key = f.owasp || '其他'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(f)
  }
  return Array.from(map.entries()).map(([owasp, items]) => ({ owasp, items }))
})

const scoreColor = computed(() => {
  const s = result.value?.risk_score ?? 100
  if (s >= 80) return '#4FB87A'
  if (s >= 50) return '#D9A857'
  return '#DC4961'
})

const scoreIcon = computed(() => {
  const s = result.value?.risk_score ?? 100
  if (s >= 80) return '🛡'
  if (s >= 50) return '⚠️'
  return '🚨'
})

async function runScan(): Promise<void> {
  if (props.source !== 'all-projects' && props.refId === null) {
    ElMessage.warning('缺少必要的 ID 参数')
    return
  }
  loading.value = true
  result.value = null
  activeFindingIdx.value = 0
  errorMessage.value = ''
  try {
    if (props.source === 'file') {
      result.value = await scanFile({
        file_id: props.refId as number,
        scan_depth: scanDepth.value,
      })
    } else if (props.source === 'task') {
      result.value = await scanTask({ task_id: props.refId as number })
    } else if (props.source === 'project') {
      result.value = await scanProject({
        project_id: props.refId as number,
        top_n: topN.value,
        trace_dataflow: traceDataflow.value,
      })
    } else {
      result.value = await scanAllProjects({
        top_n_per_project: topN.value,
        trace_dataflow: traceDataflow.value,
      })
    }
    if (!result.value?.findings?.length) {
      ElMessage.success('未检出安全风险')
    }
  } catch (e: unknown) {
    const err = e as { message?: string }
    errorMessage.value = err.message || '扫描失败'
  } finally {
    loading.value = false
  }
}

function downloadReport(): void {
  if (!result.value) return
  const lines: string[] = []
  lines.push(`# 棱镜 Prism · 安全审计报告`)
  lines.push('')
  lines.push(`- 扫描范围: ${scopeLabel.value} ${props.refName ? `(${props.refName})` : ''}`)
  lines.push(`- 风险评分: **${result.value.risk_score}/100**`)
  lines.push(`- 扫描文件数: ${result.value.file_count}`)
  lines.push(`- 耗时: ${(result.value.duration_ms / 1000).toFixed(2)}s`)
  lines.push('')
  lines.push(`## 概要`)
  lines.push(result.value.summary)
  lines.push('')
  const endpoints = result.value.threat_model?.api_endpoints ?? []
  if (endpoints.length) {
    lines.push(`## 接口扫描 (${endpoints.length} 个)`)
    for (const endpoint of endpoints) {
      lines.push(
        `- ${endpoint.method} ${endpoint.path} · ` +
          `\`${endpoint.file_path}:${endpoint.line_number}\` · ${endpoint.auth_hint}`,
      )
    }
    lines.push('')
  }
  const codeLinks = result.value.threat_model?.code_links ?? []
  if (codeLinks.length) {
    lines.push(`## 代码联动关系 (${codeLinks.length} 条)`)
    for (const link of codeLinks) {
      lines.push(
        `- [${link.severity}] ${link.relation} · ${link.risk_type}: ` +
          `${link.from} → ${link.to}`,
      )
    }
    lines.push('')
  }
  if (result.value.discussion) {
    lines.push('## 多 Agent 讨论结论')
    lines.push(result.value.discussion.consensus)
    lines.push('')
    for (const turn of result.value.discussion.turns) {
      lines.push(`- ${turn.agent_name}: ${turn.content}`)
    }
    if (result.value.discussion.action_items.length) {
      lines.push('')
      lines.push('### 行动项')
      for (const item of result.value.discussion.action_items) {
        lines.push(`- ${item}`)
      }
    }
    lines.push('')
  }
  lines.push(
    `## 严重度分布\n严重 ${severityCounts.value.严重} · ` +
      `高 ${severityCounts.value.高} · ` +
      `中 ${severityCounts.value.中} · ` +
      `低 ${severityCounts.value.低}`,
  )
  lines.push('')
  for (const group of groupedByOwasp.value) {
    lines.push(`## ${group.owasp}`)
    for (const f of group.items) {
      lines.push(`### [${f.severity}] ${f.title}`)
      lines.push(`- 位置: \`${f.file_path}\` · ${f.lines}`)
      if (f.cwe) lines.push(`- CWE: ${f.cwe}`)
      if (f.confidence < 1) lines.push(`- 置信度: ${(f.confidence * 100).toFixed(0)}%`)
      if (f.evidence) {
        lines.push('- 证据:')
        lines.push('```')
        lines.push(f.evidence)
        lines.push('```')
      }
      if (f.exploit_scenario) {
        lines.push(`- 攻击场景: ${f.exploit_scenario}`)
      }
      if (f.fix_suggestion) {
        lines.push(`- 修复建议: ${f.fix_suggestion}`)
      }
      lines.push('')
    }
  }
  const flows = result.value.threat_model?.data_flows ?? []
  if (flows.length) {
    lines.push(`## 跨文件数据流 (${flows.length} 条)`)
    for (const flow of flows) {
      const path = [flow.from, ...flow.via, flow.to].filter(Boolean).join(' → ')
      lines.push(`- [${flow.severity}] ${flow.risk_type}: ${path}`)
    }
  }
  const md = lines.join('\n')
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `prism_security_${props.source}_${props.refId ?? 'all'}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function severityClass(sev: string): string {
  return `sev-${sev}`
}

function dataflowPath(f: DataFlowOut): string {
  return [f.from, ...f.via, f.to].filter(Boolean).join(' → ')
}

function endpointLocation(endpoint: ApiEndpointOut): string {
  return `${endpoint.file_path}:${endpoint.line_number}`
}

function codeLinkPath(link: CodeLinkOut): string {
  return [link.from, link.to].filter(Boolean).join(' → ')
}

watch(visible, (v) => {
  if (v && props.autoStart && !result.value) {
    runScan()
  }
})

watch(() => props.refId, () => {
  result.value = null
  if (visible.value && props.autoStart) runScan()
})
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="dialogTitle"
    width="880px"
    :close-on-click-modal="false"
    top="5vh"
  >
    <!-- 顶部配置区 -->
    <div class="sec-toolbar">
      <div class="tb-left">
        <template v-if="source === 'file'">
          <span class="tb-label">扫描深度</span>
          <el-radio-group v-model="scanDepth" size="small">
            <el-radio-button value="quick">快速</el-radio-button>
            <el-radio-button value="standard">标准</el-radio-button>
            <el-radio-button value="deep">深度</el-radio-button>
          </el-radio-group>
        </template>

        <template v-else-if="isProjectScan">
          <span class="tb-label">
            {{ source === 'all-projects' ? '每项目文件数' : '扫描文件数' }}
          </span>
          <el-input-number v-model="topN" :min="1" :max="200" size="small" />
          <el-checkbox v-model="traceDataflow" size="small">跨文件数据流追踪</el-checkbox>
        </template>

        <template v-else>
          <span class="tb-label-static">
            <el-icon><View /></el-icon>
            任务级复审,自动给已检出的安全 issue 打 OWASP/CWE 标签
          </span>
        </template>
      </div>
      <div class="tb-right">
        <el-button
          size="small"
          type="primary"
          :icon="Lock"
          :loading="loading"
          @click="runScan"
        >
          {{ result ? '重新扫描' : '开始扫描' }}
        </el-button>
        <el-button
          v-if="result"
          size="small"
          :icon="Download"
          @click="downloadReport"
        >
          下载报告
        </el-button>
      </div>
    </div>

    <!-- loading -->
    <div v-if="loading" class="sec-loading">
      <div class="loading-icon">🛡</div>
      <div class="loading-text">
        {{
          source === 'all-projects'
            ? '正在扫描全部可见项目、接口和代码联动关系'
            : source === 'project'
              ? '正在扫描项目文件并构建威胁模型'
              : '正在执行网络安全审查'
        }}
      </div>
      <div class="loading-sub">
        {{
          isProjectScan
            ? '逐项目/逐文件执行正则、静态规则、接口抽取、数据流追踪和多 Agent 讨论摘要'
            : '正则秘钥扫描 + LLM 深度漏洞审查'
        }}
      </div>
    </div>

    <!-- 错误 -->
    <div v-else-if="errorMessage" class="sec-error">
      {{ errorMessage }}
    </div>

    <!-- 结果 -->
    <div v-else-if="result" class="sec-result">
      <!-- 评分 + 严重度 -->
      <div class="sec-score-bar">
        <div class="score-card">
          <div class="score-num font-display" :style="{ color: scoreColor }">
            <span class="score-icon">{{ scoreIcon }}</span>
            {{ result.risk_score }}
          </div>
          <div class="score-sub">风险评分 (越高越安全)</div>
        </div>
        <div class="sev-grid">
          <div class="sev-cell sev-严重">
            <div class="cell-num font-display">{{ severityCounts.严重 }}</div>
            <div class="cell-label">严重</div>
          </div>
          <div class="sev-cell sev-高">
            <div class="cell-num font-display">{{ severityCounts.高 }}</div>
            <div class="cell-label">高</div>
          </div>
          <div class="sev-cell sev-中">
            <div class="cell-num font-display">{{ severityCounts.中 }}</div>
            <div class="cell-label">中</div>
          </div>
          <div class="sev-cell sev-低">
            <div class="cell-num font-display">{{ severityCounts.低 }}</div>
            <div class="cell-label">低</div>
          </div>
          <div class="sev-cell sev-files">
            <div class="cell-num font-display">{{ result.file_count }}</div>
            <div class="cell-label">扫描文件</div>
          </div>
        </div>
      </div>

      <div class="sec-summary" v-if="result.summary">
        {{ result.summary }}
      </div>

      <!-- 接口、联动和多 Agent 讨论 -->
      <div
        v-if="
          result.threat_model &&
          (result.threat_model.api_endpoints.length ||
            result.threat_model.code_links.length ||
            result.discussion)
        "
        class="sec-threat-grid"
      >
        <section
          v-if="result.threat_model.api_endpoints.length"
          class="threat-panel"
        >
          <header class="block-head compact">
            <span class="block-title">接口扫描</span>
            <span class="block-count font-mono">
              {{ result.threat_model.api_endpoints.length }}
            </span>
          </header>
          <ul class="endpoint-list">
            <li
              v-for="endpoint in result.threat_model.api_endpoints"
              :key="`${endpoint.method}-${endpoint.path}-${endpoint.file_path}-${endpoint.line_number}`"
              class="endpoint-row"
            >
              <span class="method-chip">{{ endpoint.method }}</span>
              <span class="endpoint-path font-mono">{{ endpoint.path }}</span>
              <span class="endpoint-file font-mono">{{ endpointLocation(endpoint) }}</span>
              <span
                class="auth-hint"
                :class="{ missing: endpoint.auth_hint.includes('未发现') }"
              >
                {{ endpoint.auth_hint }}
              </span>
            </li>
          </ul>
        </section>

        <section
          v-if="result.threat_model.code_links.length"
          class="threat-panel"
        >
          <header class="block-head compact">
            <span class="block-title">代码联动关系</span>
            <span class="block-count font-mono">
              {{ result.threat_model.code_links.length }}
            </span>
          </header>
          <ul class="link-list">
            <li
              v-for="(link, idx) in result.threat_model.code_links"
              :key="`${link.from}-${link.to}-${idx}`"
              class="link-row"
            >
              <span class="sev-chip" :class="severityClass(link.severity)">
                {{ link.severity }}
              </span>
              <div class="link-info">
                <div class="link-title">{{ link.relation }} · {{ link.risk_type }}</div>
                <div class="link-path font-mono">{{ codeLinkPath(link) }}</div>
              </div>
            </li>
          </ul>
        </section>

        <section
          v-if="result.discussion"
          class="threat-panel discussion-panel"
        >
          <header class="block-head compact">
            <span class="block-title">多 Agent 讨论</span>
            <span class="block-count font-mono">
              {{ result.discussion.turns.length }}
            </span>
          </header>
          <div class="discussion-consensus">{{ result.discussion.consensus }}</div>
          <ul class="discussion-turns">
            <li
              v-for="turn in result.discussion.turns"
              :key="`${turn.agent_code}-${turn.role}`"
              class="discussion-turn"
            >
              <span class="agent-name">{{ turn.agent_name }}</span>
              <span class="agent-content">{{ turn.content }}</span>
            </li>
          </ul>
          <ul
            v-if="result.discussion.action_items.length"
            class="action-list"
          >
            <li
              v-for="item in result.discussion.action_items"
              :key="item"
            >
              {{ item }}
            </li>
          </ul>
        </section>
      </div>

      <!-- findings 列表 -->
      <div v-if="sortedFindings.length" class="sec-findings">
        <header class="block-head">
          <span class="block-title">安全发现</span>
          <span class="block-count font-mono">{{ sortedFindings.length }}</span>
        </header>
        <div class="findings-grid">
          <aside class="findings-list">
            <template v-for="group in groupedByOwasp" :key="group.owasp">
              <div class="group-head">{{ group.owasp || '其他' }}</div>
              <button
                v-for="f in group.items"
                :key="`${f.file_path}-${f.line_number}-${f.title}`"
                class="finding-row"
                :class="{
                  active:
                    sortedFindings[activeFindingIdx] &&
                    sortedFindings[activeFindingIdx].file_path === f.file_path &&
                    sortedFindings[activeFindingIdx].line_number === f.line_number &&
                    sortedFindings[activeFindingIdx].title === f.title,
                }"
                @click="activeFindingIdx = sortedFindings.indexOf(f)"
              >
                <span class="sev-chip" :class="severityClass(f.severity)">{{ f.severity }}</span>
                <div class="finding-info">
                  <div class="finding-title">{{ f.title }}</div>
                  <div class="finding-sub font-mono">
                    {{ f.file_path }} · {{ f.lines }}
                  </div>
                </div>
              </button>
            </template>
          </aside>

          <main class="finding-view" v-if="sortedFindings[activeFindingIdx]">
            <header class="view-head">
              <div class="view-title">
                <span class="sev-chip" :class="severityClass(sortedFindings[activeFindingIdx].severity)">
                  {{ sortedFindings[activeFindingIdx].severity }}
                </span>
                <span>{{ sortedFindings[activeFindingIdx].title }}</span>
              </div>
              <div class="view-tags">
                <el-tag v-if="sortedFindings[activeFindingIdx].owasp" size="small" type="danger">
                  {{ sortedFindings[activeFindingIdx].owasp }}
                </el-tag>
                <el-tag v-if="sortedFindings[activeFindingIdx].cwe" size="small" type="info">
                  {{ sortedFindings[activeFindingIdx].cwe }}
                </el-tag>
                <el-tag
                  v-if="sortedFindings[activeFindingIdx].source"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  {{ sortedFindings[activeFindingIdx].source === 'regex' ? '正则' : 'LLM' }}
                </el-tag>
              </div>
            </header>
            <dl class="view-body">
              <dt>位置</dt>
              <dd class="font-mono">
                {{ sortedFindings[activeFindingIdx].file_path }} · {{ sortedFindings[activeFindingIdx].lines }}
              </dd>
              <dt v-if="sortedFindings[activeFindingIdx].confidence < 1">置信度</dt>
              <dd v-if="sortedFindings[activeFindingIdx].confidence < 1">
                {{ (sortedFindings[activeFindingIdx].confidence * 100).toFixed(0) }}%
              </dd>
              <dt v-if="sortedFindings[activeFindingIdx].evidence">证据(脱敏)</dt>
              <dd v-if="sortedFindings[activeFindingIdx].evidence">
                <pre class="evidence">{{ sortedFindings[activeFindingIdx].evidence }}</pre>
              </dd>
              <dt v-if="sortedFindings[activeFindingIdx].exploit_scenario">攻击场景</dt>
              <dd v-if="sortedFindings[activeFindingIdx].exploit_scenario">
                {{ sortedFindings[activeFindingIdx].exploit_scenario }}
              </dd>
              <dt v-if="sortedFindings[activeFindingIdx].fix_suggestion">修复建议</dt>
              <dd v-if="sortedFindings[activeFindingIdx].fix_suggestion">
                {{ sortedFindings[activeFindingIdx].fix_suggestion }}
              </dd>
              <dt v-if="sortedFindings[activeFindingIdx].references.length">参考</dt>
              <dd v-if="sortedFindings[activeFindingIdx].references.length">
                <a
                  v-for="r in sortedFindings[activeFindingIdx].references"
                  :key="r"
                  :href="r"
                  target="_blank"
                  rel="noopener"
                  class="ref-link"
                >
                  {{ r }}
                </a>
              </dd>
            </dl>
          </main>
        </div>
      </div>

      <!-- 跨文件数据流 -->
      <div
        v-if="result.threat_model && result.threat_model.data_flows.length"
        class="sec-dataflows"
      >
        <header class="block-head">
          <span class="block-title">跨文件攻击路径</span>
          <span class="block-count font-mono">
            {{ result.threat_model.data_flows.length }}
          </span>
        </header>
        <div class="flow-summary" v-if="result.threat_model.attack_surface_summary">
          {{ result.threat_model.attack_surface_summary }}
        </div>
        <ul class="flow-list">
          <li
            v-for="(flow, idx) in result.threat_model.data_flows"
            :key="idx"
            class="flow-row"
          >
            <span class="sev-chip" :class="severityClass(flow.severity)">{{ flow.severity }}</span>
            <span class="flow-type">{{ flow.risk_type }}</span>
            <span class="flow-path font-mono">{{ dataflowPath(flow) }}</span>
          </li>
        </ul>
      </div>
    </div>

    <!-- 初始空态 -->
    <div v-else class="sec-empty">
      <div class="empty-icon">🛡</div>
      <div class="empty-text">点击右上角「开始扫描」启动安全审计</div>
      <div class="empty-sub">
        将由 <code>security_sentinel</code> Agent 负责执行,
        包含正则秘钥识别 + LLM 深度漏洞审查
        <span v-if="isProjectScan"> + 跨文件数据流追踪</span>
      </div>
      <el-button type="primary" :icon="RefreshRight" :loading="loading" @click="runScan">
        开始扫描
      </el-button>
    </div>
  </el-dialog>
</template>

<style scoped lang="scss">
.sec-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--gray-100);
  margin-bottom: 12px;
}

.tb-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;

  .tb-label {
    font-size: 12px;
    color: var(--gray-600);
  }
  .tb-label-static {
    font-size: 12px;
    color: var(--gray-600);
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
}

.tb-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sec-loading,
.sec-empty {
  text-align: center;
  padding: 50px 20px;
}

.loading-icon,
.empty-icon {
  font-size: 36px;
  margin-bottom: 12px;
}

.loading-text {
  font-size: 14px;
  color: var(--gray-700);
  margin-bottom: 4px;
}

.loading-sub,
.empty-sub {
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 16px;
}

.empty-text {
  font-size: 14px;
  color: var(--gray-700);
  margin-bottom: 6px;
}

.sec-error {
  background: rgba(220, 73, 97, 0.08);
  border-left: 3px solid #DC4961;
  padding: 10px 12px;
  font-size: 13px;
  color: #DC4961;
  border-radius: 4px;
  margin: 12px 0;
}

.sec-score-bar {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.score-card {
  background: var(--gray-50);
  border-radius: 10px;
  padding: 18px;
  text-align: center;
  display: flex;
  flex-direction: column;
  justify-content: center;

  .score-num {
    font-size: 44px;
    font-weight: 600;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    justify-content: center;
  }
  .score-icon { font-size: 24px; }
  .score-sub {
    margin-top: 8px;
    font-size: 11px;
    color: var(--gray-500);
  }
}

.sev-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 8px;
}

.sev-cell {
  background: var(--gray-50);
  border-radius: 8px;
  padding: 14px 8px;
  text-align: center;

  .cell-num {
    font-size: 22px;
    font-weight: 600;
  }
  .cell-label {
    font-size: 11px;
    color: var(--gray-600);
    margin-top: 2px;
  }
}

.sev-cell.sev-严重 .cell-num { color: #DC4961; }
.sev-cell.sev-高 .cell-num { color: #E27C4A; }
.sev-cell.sev-中 .cell-num { color: #D9A857; }
.sev-cell.sev-低 .cell-num { color: #4FB87A; }
.sev-cell.sev-files .cell-num { color: var(--brand-600, #5B58E8); }

.sec-summary {
  background: rgba(217, 59, 59, 0.06);
  border-left: 3px solid #D93B3B;
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 12.5px;
  color: var(--gray-800);
  margin-bottom: 12px;
}

.block-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 8px;
}
.block-title { font-size: 13px; font-weight: 600; color: var(--gray-800); }
.block-count {
  font-size: 11px;
  color: var(--gray-500);
  background: var(--gray-100);
  padding: 1px 6px;
  border-radius: 8px;
}

.findings-grid {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 12px;
  min-height: 280px;
  max-height: 48vh;
}

.findings-list {
  background: var(--gray-50);
  border-radius: 8px;
  padding: 6px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.group-head {
  font-size: 10.5px;
  color: var(--gray-500);
  padding: 4px 8px 2px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 4px;
}

.finding-row {
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 6px 8px;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px;
  align-items: flex-start;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover { background: #fff; }
  &.active {
    background: #fff;
    border-color: #D93B3B;
    box-shadow: 0 1px 4px rgba(217, 59, 59, 0.15);
  }
}

.sev-chip {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  color: #fff;
  font-weight: 600;
  white-space: nowrap;

  &.sev-严重 { background: #DC4961; }
  &.sev-高 { background: #E27C4A; }
  &.sev-中 { background: #D9A857; }
  &.sev-低 { background: #4FB87A; }
}

.finding-info { min-width: 0; }

.finding-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--gray-900);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.finding-sub {
  font-size: 10.5px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.finding-view {
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #fff;
}

.view-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  background: var(--gray-50);
  border-bottom: 1px solid var(--gray-100);
  flex-wrap: wrap;
}

.view-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
}

.view-tags {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.view-body {
  margin: 0;
  padding: 12px 14px;
  font-size: 12.5px;
  line-height: 1.6;
  overflow-y: auto;

  dt {
    color: var(--gray-500);
    font-size: 11px;
    margin-top: 8px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  dt:first-child { margin-top: 0; }
  dd {
    margin: 2px 0 0;
    color: var(--gray-800);
  }
}

.evidence {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: var(--gray-50);
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
}

.ref-link {
  display: block;
  color: var(--brand-600, #5B58E8);
  text-decoration: none;
  font-size: 11.5px;
  word-break: break-all;
  margin-top: 2px;

  &:hover { text-decoration: underline; }
}

.sec-threat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 12px 0;
}

.threat-panel {
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
  min-width: 0;
}

.discussion-panel {
  grid-column: 1 / -1;
}

.block-head.compact {
  margin: 0 0 8px;
}

.endpoint-list,
.link-list,
.discussion-turns,
.action-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.endpoint-list,
.link-list,
.discussion-turns {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 160px;
  overflow-y: auto;
}

.endpoint-row {
  display: grid;
  grid-template-columns: auto minmax(90px, 1fr);
  gap: 4px 8px;
  align-items: center;
  padding: 6px 8px;
  background: var(--gray-50);
  border-radius: 6px;
}

.method-chip {
  font-size: 10px;
  font-weight: 700;
  color: #fff;
  background: var(--brand-600, #5B58E8);
  border-radius: 3px;
  padding: 2px 6px;
}

.endpoint-path {
  font-size: 11px;
  color: var(--gray-900);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.endpoint-file {
  grid-column: 1 / -1;
  font-size: 10.5px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.auth-hint {
  grid-column: 1 / -1;
  font-size: 11px;
  color: #4FB87A;

  &.missing {
    color: #D9A857;
  }
}

.link-row {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px;
  align-items: start;
  padding: 6px 8px;
  background: var(--gray-50);
  border-radius: 6px;
}

.link-info {
  min-width: 0;
}

.link-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--gray-800);
}

.link-path {
  font-size: 10.5px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.discussion-consensus {
  background: rgba(91, 88, 232, 0.08);
  border-left: 3px solid var(--brand-600, #5B58E8);
  border-radius: 4px;
  color: var(--gray-800);
  font-size: 12px;
  line-height: 1.6;
  padding: 7px 9px;
  margin-bottom: 8px;
}

.discussion-turn {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--gray-100);
  font-size: 12px;
}

.agent-name {
  color: var(--gray-700);
  font-weight: 600;
}

.agent-content {
  color: var(--gray-600);
  line-height: 1.5;
}

.action-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;

  li {
    color: var(--gray-800);
    font-size: 12px;
    line-height: 1.5;
    padding-left: 12px;
    position: relative;

    &::before {
      content: '';
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: #D93B3B;
      position: absolute;
      left: 0;
      top: 8px;
    }
  }
}

.sec-dataflows {
  margin-top: 12px;
}

.flow-summary {
  font-size: 12px;
  color: var(--gray-600);
  margin-bottom: 6px;
}

.flow-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.flow-row {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  background: var(--gray-50);
  border-radius: 6px;
  font-size: 12px;

  .flow-type {
    color: var(--gray-800);
    font-weight: 500;
  }
  .flow-path {
    font-size: 11px;
    color: var(--gray-600);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
