<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ArrowRight, Lock, TrendCharts } from '@element-plus/icons-vue'
import { getSecurityChecklist, getSecurityDashboard } from '@/api/security'
import type {
  SecurityChecklistItem,
  SecurityChecklistOut,
  SecurityDashboardSummaryOut,
} from '@/types/security'
import SecurityScanModal from '@/components/security/SecurityScanModal.vue'
import { OWASP_TOP10, type OwaspDoc } from './owasp-knowledge'
import { useUserStore } from '@/stores/user'
import { ElMessage } from 'element-plus/es/components/message/index'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const checklist = ref<SecurityChecklistOut | null>(null)
const dashboardData = ref<SecurityDashboardSummaryOut | null>(null)
const dashboardLoading = ref(false)
const selectedOwasp = ref<OwaspDoc | null>(null)
const detailVisible = ref(false)
const securityScanVisible = ref(false)

const secretCount = computed(() => checklist.value?.secret_patterns.length ?? 0)
const staticCount = computed(() => checklist.value?.static_rules.length ?? 0)
const canScan = computed(() => userStore.hasPermission('security:scan'))
const canViewProjects = computed(() => userStore.hasPermission('project:view'))
const canCreateProject = computed(() => userStore.hasPermission('project:create'))

const scoreColor = computed(() => {
  const s = dashboardData.value?.avg_risk_score
  if (s === null || s === undefined) return 'var(--gray-400)'
  if (s >= 80) return '#4FB87A'
  if (s >= 50) return '#D9A857'
  return '#DC4961'
})

const hasProjectData = computed(() => {
  return dashboardData.value && dashboardData.value.project_count > 0
})

const hasScannedData = computed(() => {
  return dashboardData.value && dashboardData.value.scanned_project_count > 0
})

const owaspColors: Record<string, string> = {
  A01: '#C92A6E',
  A02: '#D93B3B',
  A03: '#E25C73',
  A04: '#E27C4A',
  A05: '#D9A857',
  A06: '#9F7AEA',
  A07: '#5B58E8',
  A08: '#4B9BFF',
  A09: '#3DBCD9',
  A10: '#2A9D8F',
}

async function loadChecklist(): Promise<void> {
  loading.value = true
  try {
    checklist.value = await getSecurityChecklist()
  } catch {
    checklist.value = null
    ElMessage.error('获取安全规则清单失败')
  } finally {
    loading.value = false
  }
}

function openOwaspDetail(owasp: OwaspDoc): void {
  selectedOwasp.value = owasp
  detailVisible.value = true
}

async function loadDashboardSummary(): Promise<void> {
  dashboardLoading.value = true
  try {
    dashboardData.value = await getSecurityDashboard(30)
  } catch {
    dashboardData.value = null
  } finally {
    dashboardLoading.value = false
  }
}

function gotoProjects(): void {
  if (!canViewProjects.value) return
  router.push('/projects')
}

function openAllProjectScan(): void {
  if (!canScan.value) return
  securityScanVisible.value = true
}

function jumpAndCloseDetail(): void {
  if (!canScan.value || !canViewProjects.value) return
  detailVisible.value = false
  router.push('/projects')
}

onMounted(() => {
  loadChecklist()
  loadDashboardSummary()
})
</script>

<template>
  <div class="security-center-page">
    <header class="page-head">
      <div>
        <h2 class="page-title font-display">
          <el-icon><Lock /></el-icon>
          安全中心
        </h2>
        <p class="page-sub">
          OWASP Top 10 知识库 ·
          <b class="hl">{{ secretCount }}</b> 类敏感信息规则 ·
          <b class="hl">{{ staticCount }}</b> 条静态语义规则 ·
          覆盖代码审查中的网络安全维度
        </p>
      </div>
      <div v-if="canScan" class="page-actions">
        <el-button type="primary" :icon="Lock" @click="openAllProjectScan">
          全量扫描
        </el-button>
        <el-button v-if="canViewProjects" :icon="ArrowRight" @click="gotoProjects">
          单项目扫描
        </el-button>
      </div>
    </header>

    <SecurityScanModal
      v-model="securityScanVisible"
      source="all-projects"
      :ref-id="null"
      ref-name="全部可见项目"
    />

    <!-- ========== 我的项目安全概览 (v2.1.1) ========== -->
    <section class="block overview" v-loading="dashboardLoading">
      <header class="block-head">
        <h3 class="block-title">
          <el-icon><TrendCharts /></el-icon>
          我的项目安全概览
        </h3>
        <p class="block-sub">近 30 天安全态势</p>
      </header>

      <div v-if="dashboardData" class="overview-body">
        <!-- 有项目但未扫描 -->
        <div v-if="hasProjectData && !hasScannedData" class="overview-hint">
          <div class="hint-icon">🛡</div>
          <div class="hint-text">
            你有 <b>{{ dashboardData.project_count }}</b> 个项目还未进行安全审计。
            <template v-if="canScan">点击「立即扫描」开始分析。</template>
          </div>
          <el-button v-if="canScan" size="small" type="primary" :icon="Lock" @click="openAllProjectScan">
            全量扫描
          </el-button>
          <el-button v-if="canScan && canViewProjects" size="small" :icon="ArrowRight" @click="gotoProjects">
            单项目扫描
          </el-button>
        </div>

        <!-- 无项目 -->
        <div v-else-if="!hasProjectData" class="overview-hint">
          <div class="hint-icon">📁</div>
          <div class="hint-text">还没有项目可分析。</div>
          <el-button v-if="canCreateProject && canViewProjects" size="small" type="primary" @click="gotoProjects">
            创建项目
          </el-button>
        </div>

        <!-- 完整数据 -->
        <div v-else class="overview-stats">
          <div class="ov-stat-cards">
            <div class="ov-card">
              <div class="ov-num font-display" :style="{ color: scoreColor }">
                {{ dashboardData.avg_risk_score === null ? '—' : dashboardData.avg_risk_score }}
              </div>
              <div class="ov-label">平均风险评分</div>
            </div>
            <div class="ov-card">
              <div class="ov-num font-display sev-severe">{{ dashboardData.severe_issues_total }}</div>
              <div class="ov-label">严重问题</div>
            </div>
            <div class="ov-card">
              <div class="ov-num font-display sev-high">{{ dashboardData.high_issues_total }}</div>
              <div class="ov-label">高危问题</div>
            </div>
            <div class="ov-card">
              <div class="ov-num font-display sev-medium">{{ dashboardData.medium_issues_total }}</div>
              <div class="ov-label">中危问题</div>
            </div>
            <div class="ov-card">
              <div class="ov-num font-display sev-low">{{ dashboardData.low_issues_total }}</div>
              <div class="ov-label">低危问题</div>
            </div>
            <div class="ov-card">
              <div class="ov-num font-display">{{ dashboardData.scanned_project_count }}<span class="ov-unit">/{{ dashboardData.project_count }}</span></div>
              <div class="ov-label">已扫描项目</div>
            </div>
          </div>

          <div v-if="dashboardData.owasp_hotspots.length" class="ov-hotspots">
            <span class="ov-hot-title">你的项目 OWASP 热点</span>
            <div class="ov-hot-list">
              <span
                v-for="(h, i) in dashboardData.owasp_hotspots.slice(0, 5)"
                :key="h.owasp"
                class="ov-hot-item"
              >
                <span class="ov-hot-rank font-mono">#{{ i + 1 }}</span>
                <span class="ov-hot-name">{{ h.owasp }}</span>
                <span class="ov-hot-count font-mono">{{ h.count }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ========== OWASP Top 10 ========== -->
    <section class="block">
      <header class="block-head">
        <h3 class="block-title">OWASP Top 10 · 2021</h3>
        <p class="block-sub">
          OWASP 基金会评选的 Web 应用最危险 10 类风险,棱镜围绕这 10 项展开扫描。
          点击任一项目可查看中文详解 + 防范代码示例。
        </p>
      </header>

      <div class="owasp-grid">
        <article
          v-for="owasp in OWASP_TOP10"
          :key="owasp.code"
          class="owasp-card"
          :style="{
            '--card-color': owaspColors[owasp.code] || '#5B58E8',
          }"
          tabindex="0"
          role="button"
          @click="openOwaspDetail(owasp)"
          @keyup.enter="openOwaspDetail(owasp)"
        >
          <div class="owasp-code">{{ owasp.code }}</div>
          <div class="owasp-name-zh">{{ owasp.name_zh }}</div>
          <div class="owasp-name-en">{{ owasp.name_en }}</div>
          <div class="owasp-cwe-chips">
            <span
              v-for="cwe in owasp.cwe_refs.slice(0, 3)"
              :key="cwe"
              class="cwe-chip font-mono"
            >
              {{ cwe }}
            </span>
            <span
              v-if="owasp.cwe_refs.length > 3"
              class="cwe-chip cwe-chip-more font-mono"
            >
              +{{ owasp.cwe_refs.length - 3 }}
            </span>
          </div>
        </article>
      </div>
    </section>

    <!-- ========== 敏感信息正则规则 ========== -->
    <section class="block">
      <header class="block-head">
        <h3 class="block-title">敏感信息识别规则</h3>
        <p class="block-sub">
          确定性正则匹配,扫描文件中的硬编码 API Key、Token、密码、私钥等。
          命中后默认 <code>severity = 严重</code>,无 LLM 调用成本。
        </p>
      </header>

      <div class="rule-table-wrap" v-loading="loading">
        <el-table
          v-if="checklist?.secret_patterns.length"
          :data="checklist.secret_patterns"
          border
          stripe
          size="small"
          empty-text="加载中..."
        >
          <el-table-column label="名称" min-width="180">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <span class="rule-name">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="CWE" width="120">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <el-tag size="small" type="info" class="font-mono">{{ row.cwe }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="OWASP" min-width="240">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <span class="font-mono small-mono">{{ row.owasp }}</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="220">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              {{ row.description }}
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <!-- ========== 静态语义规则 ========== -->
    <section class="block">
      <header class="block-head">
        <h3 class="block-title">静态语义规则</h3>
        <p class="block-sub">
          形态识别:弱加密算法 (MD5/SHA1/DES/ECB)、危险 API (eval/pickle.loads)、
          HTTP 响应头缺失。也无 LLM 成本,确定性命中。
        </p>
      </header>

      <div class="rule-table-wrap" v-loading="loading">
        <el-table
          v-if="checklist?.static_rules.length"
          :data="checklist.static_rules"
          border
          stripe
          size="small"
          empty-text="加载中..."
        >
          <el-table-column label="代号" width="200">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <span class="font-mono small-mono">{{ row.code }}</span>
            </template>
          </el-table-column>
          <el-table-column label="规则名" min-width="180">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <span class="rule-name">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="CWE" width="120">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              <el-tag size="small" type="info" class="font-mono">{{ row.cwe }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="220">
            <template #default="{ row }: { row: SecurityChecklistItem }">
              {{ row.description }}
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <!-- ========== 用法指引 ========== -->
    <section v-if="canScan" class="block usage">
      <h3 class="block-title">我能帮你做什么</h3>
      <ul class="usage-list">
        <li>
          <span class="usage-glyph">🛡</span>
          <div>
            <b>全量项目安全扫描</b>
            <span>
              在本页点击 <kbd>全量扫描</kbd>,
              对当前账号可见的所有活跃项目统一执行安全审计并聚合风险。
            </span>
          </div>
        </li>
        <li>
          <span class="usage-glyph">◎</span>
          <div>
            <b>单项目威胁建模</b>
            <span>
              在「项目详情」点 <kbd>🛡 安全审计</kbd>,
              获得跨文件数据流追踪 + OWASP/CWE 标签 + 风险评分。
            </span>
          </div>
        </li>
        <li>
          <span class="usage-glyph">🔁</span>
          <div>
            <b>任务级安全复审</b>
            <span>
              在「审查详情」点 <kbd>🛡 安全复审</kbd>,
              给已检出的安全 issue 自动打 OWASP / CWE 标签。
            </span>
          </div>
        </li>
        <li>
          <span class="usage-glyph">💬</span>
          <div>
            <b>自然语言入口</b>
            <span>
              在 Agent 助手中说 <kbd>"对项目 12 做安全审计"</kbd>,
              <code>security_sentinel</code> Agent 会自动完成扫描。
            </span>
          </div>
        </li>
      </ul>
    </section>

    <!-- ========== OWASP 详细页 Dialog ========== -->
    <el-dialog
      v-model="detailVisible"
      width="780px"
      top="5vh"
      :close-on-click-modal="false"
      :title="selectedOwasp ? `${selectedOwasp.code} · ${selectedOwasp.name_zh}` : ''"
    >
      <div v-if="selectedOwasp" class="owasp-detail">
        <div class="detail-subtitle">{{ selectedOwasp.name_en }}</div>

        <section class="detail-section">
          <h4>定义</h4>
          <p>{{ selectedOwasp.definition }}</p>
        </section>

        <section class="detail-section">
          <h4>危害</h4>
          <p>{{ selectedOwasp.harm }}</p>
        </section>

        <section class="detail-section">
          <h4>❌ 反面示例</h4>
          <pre class="code-block bad">{{ selectedOwasp.bad_example.code }}</pre>
        </section>

        <section class="detail-section">
          <h4>✅ 正确做法</h4>
          <pre class="code-block good">{{ selectedOwasp.good_example.code }}</pre>
        </section>

        <section class="detail-section">
          <h4>防范要点</h4>
          <ul class="prev-list">
            <li v-for="(p, i) in selectedOwasp.prevention" :key="i">{{ p }}</li>
          </ul>
        </section>

        <section class="detail-section">
          <h4>关联 CWE</h4>
          <div class="cwe-bar">
            <el-tag
              v-for="cwe in selectedOwasp.cwe_refs"
              :key="cwe"
              size="small"
              type="info"
              class="font-mono"
              effect="plain"
            >
              {{ cwe }}
            </el-tag>
          </div>
        </section>

        <section class="detail-section coverage">
          <h4>棱镜如何检测</h4>
          <p>{{ selectedOwasp.prism_coverage }}</p>
        </section>
      </div>

      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button v-if="canScan && canViewProjects" type="primary" :icon="Lock" @click="jumpAndCloseDetail">
          🛡 扫描我的项目
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.security-center-page {
  padding: 16px 0 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 12px;
}

.page-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 22px;
  font-weight: 600;
  color: var(--gray-900);
  margin: 0;
}

.page-sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--gray-600);

  .hl {
    color: #D93B3B;
    font-weight: 600;
  }
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.block {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.block-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.block-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
  margin: 0;
}

.block-sub {
  font-size: 12.5px;
  color: var(--gray-600);
  margin: 0;

  code {
    background: var(--gray-100);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11.5px;
  }
}

/* ===== 概览 ===== */
.overview {
  background: #fff;
}

.overview-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.overview-hint {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 20px 24px;
  background: linear-gradient(135deg, rgba(91, 88, 232, 0.04), rgba(217, 59, 59, 0.04));
  border: 1px solid var(--gray-100);
  border-radius: 10px;
}

.hint-icon {
  font-size: 32px;
  line-height: 1;
}

.hint-text {
  flex: 1;
  font-size: 13px;
  color: var(--gray-800);
  line-height: 1.6;
}

.hint-text b {
  color: var(--gray-900);
  font-weight: 600;
}

.overview-stats {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ov-stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}

.ov-card {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ov-num {
  font-size: 24px;
  font-weight: 700;
  color: var(--gray-900);
}

.ov-unit {
  font-size: 14px;
  color: var(--gray-400);
  font-weight: 400;
}

.ov-label {
  font-size: 12px;
  color: var(--gray-600);
}

.sev-severe { color: #DC4961 !important; }
.sev-high { color: #E27C4A !important; }
.sev-medium { color: #D9A857 !important; }
.sev-low { color: #4FB87A !important; }

/* ---- OWASP 热点 ---- */
.ov-hotspots {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ov-hot-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--gray-700);
}

.ov-hot-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ov-hot-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: rgba(217, 59, 59, 0.06);
  border-radius: 16px;
  font-size: 12px;
}

.ov-hot-rank {
  font-size: 10px;
  color: var(--gray-500);
}

.ov-hot-name {
  color: var(--gray-800);
  font-size: 11.5px;
}

.ov-hot-count {
  color: #D93B3B;
  font-weight: 600;
}

/* ===== OWASP 卡片 ===== */
.owasp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.owasp-card {
  position: relative;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 10px;
  padding: 16px 14px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: all 0.15s ease;
  outline: none;
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 4px;
    height: 100%;
    background: var(--card-color);
  }

  &:hover,
  &:focus {
    transform: translateY(-2px);
    border-color: var(--card-color);
    box-shadow: 0 4px 14px rgba(217, 59, 59, 0.12);
  }
}

.owasp-code {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--card-color);
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
}

.owasp-name-zh {
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-900);
  line-height: 1.25;
}

.owasp-name-en {
  font-size: 11px;
  color: var(--gray-500);
  font-family: 'JetBrains Mono', monospace;
}

.owasp-cwe-chips {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.cwe-chip {
  background: var(--gray-100);
  color: var(--gray-700);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
}

.cwe-chip-more {
  background: transparent;
  color: var(--gray-400);
}

/* ===== 表格 ===== */
.rule-table-wrap {
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--gray-100);
}

.rule-name {
  font-weight: 500;
  color: var(--gray-900);
}

.small-mono {
  font-size: 11px;
  color: var(--gray-600);
}

/* ===== 用法 ===== */
.usage {
  background: linear-gradient(135deg, rgba(217, 59, 59, 0.05), rgba(91, 88, 232, 0.05));
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  padding: 20px 24px;
}

.usage-list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.usage-list li {
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 12px;
  align-items: flex-start;
}

.usage-glyph {
  font-size: 22px;
  line-height: 1;
}

.usage-list b {
  display: block;
  font-size: 13.5px;
  color: var(--gray-900);
  margin-bottom: 2px;
}

.usage-list span {
  font-size: 12.5px;
  color: var(--gray-700);
  line-height: 1.6;
}

.usage-list kbd {
  background: #fff;
  border: 1px solid var(--gray-200);
  border-radius: 4px;
  padding: 1px 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px;
  color: var(--gray-800);
  box-shadow: 0 1px 0 var(--gray-200);
}

.usage-list code {
  background: var(--gray-100);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
}

/* ===== Dialog 详细页 ===== */
.detail-subtitle {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 16px;
}

.detail-section {
  margin-bottom: 16px;
}

.detail-section h4 {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
}

.detail-section p {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--gray-800);
}

.prev-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--gray-800);
}

.code-block {
  margin: 0;
  padding: 12px 14px;
  background: var(--gray-50);
  border-radius: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--gray-800);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}

.code-block.bad {
  border-left: 3px solid #DC4961;
}

.code-block.good {
  border-left: 3px solid #4FB87A;
}

.cwe-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.detail-section.coverage {
  background: rgba(217, 59, 59, 0.06);
  border-left: 3px solid #D93B3B;
  border-radius: 4px;
  padding: 10px 14px;
}

.detail-section.coverage h4,
.detail-section.coverage p {
  color: var(--gray-900);
}
</style>
