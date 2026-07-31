<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, FolderOpened, Lock } from '@element-plus/icons-vue'
import { getSecurityDashboard } from '@/api/security'
import type { SecurityDashboardSummaryOut, TrendPointOut } from '@/types/security'

interface Props {
  days?: number
}

const props = withDefaults(defineProps<Props>(), {
  days: 30,
})

const router = useRouter()
const loading = ref(false)
const data = ref<SecurityDashboardSummaryOut | null>(null)
const error = ref('')

const scoreColor = computed(() => {
  const s = data.value?.avg_risk_score
  if (s === null || s === undefined) return 'var(--gray-400)'
  if (s >= 80) return '#4FB87A'
  if (s >= 50) return '#D9A857'
  return '#DC4961'
})

const scoreText = computed(() => {
  const s = data.value?.avg_risk_score
  return s === null || s === undefined ? '—' : String(s)
})

const isEmpty = computed(() => {
  if (!data.value) return false
  return data.value.project_count === 0
})

const isUnScanned = computed(() => {
  if (!data.value || data.value.project_count === 0) return false
  return data.value.scanned_project_count === 0
})

const totalIssues = computed(() => {
  if (!data.value) return 0
  return (
    data.value.severe_issues_total +
    data.value.high_issues_total +
    data.value.medium_issues_total +
    data.value.low_issues_total
  )
})

/**
 * 将趋势数据转换为 SVG 折线的点串
 * @param trend - 趋势数据点数组
 * @param width - SVG 宽度
 * @param height - SVG 高度
 * @param padding - 内边距
 * @returns SVG polyline points 字符串
 */
function sparklinePoints(
  trend: TrendPointOut[],
  width: number,
  height: number,
  padding: number,
): string {
  if (trend.length < 2) return ''
  const maxVal = Math.max(1, ...trend.map((t) => t.severe + t.high))
  const stepX = (width - padding * 2) / (trend.length - 1)
  return trend
    .map((t, i) => {
      const x = padding + i * stepX
      const y = height - padding - ((t.severe + t.high) / maxVal) * (height - padding * 2)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

/**
 * 构建 SVG 面积路径（折线 + 底部闭合）。
 * @param trend - 趋势数据点数组
 * @param width - SVG 宽度
 * @param height - SVG 高度
 * @param padding - 内边距
 * @returns SVG path 字符串
 */
function sparklineArea(
  trend: TrendPointOut[],
  width: number,
  height: number,
  padding: number,
): string {
  if (trend.length < 2) return ''
  const maxVal = Math.max(1, ...trend.map((t) => t.severe + t.high))
  const stepX = (width - padding * 2) / (trend.length - 1)
  const points = trend.map((t, i) => {
    const x = padding + i * stepX
    const y = height - padding - ((t.severe + t.high) / maxVal) * (height - padding * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  const firstPoint = points[0]
  const lastPoint = points[points.length - 1]
  const lastX = lastPoint.split(',')[0]
  return `M${points.join(' L')} L${lastX},${height - padding} L${firstPoint.split(',')[0]},${height - padding} Z`
}

/**
 * 加载安全态势；失败时保留可重试错误状态。
 * @returns 无返回值，结果写入组件状态
 */
async function loadDashboard(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    data.value = await getSecurityDashboard(props.days)
  } catch (e: unknown) {
    const err = e as { message?: string }
    error.value = err.message || '加载安全态势失败'
  } finally {
    loading.value = false
  }
}

/** 跳转安全中心。
 * @returns 无返回值
 */
function gotoSecurityCenter(): void {
  router.push('/security')
}

/** 跳转项目详情。
 * @param projectId - 项目 ID
 * @returns 无返回值
 */
function gotoProject(projectId: number): void {
  router.push(`/projects/${projectId}`)
}

/** 跳转项目列表以创建项目。
 * @returns 无返回值
 */
function gotoCreateProject(): void {
  router.push('/projects')
}

/** 跳转项目列表以发起扫描。
 * @returns 无返回值
 */
function gotoProjectList(): void {
  router.push('/projects')
}

onMounted(loadDashboard)
</script>

<template>
  <article class="posture-card" v-loading="loading">
    <header class="card-head">
      <div class="head-title">
        <span class="head-mark prism-mark sm on-light"></span>
        <h3 class="font-display">项目安全态势</h3>
        <span class="head-range font-mono">近 {{ days }} 天</span>
      </div>
      <a class="card-link" @click="gotoSecurityCenter">
        安全中心 <el-icon><ArrowRight /></el-icon>
      </a>
    </header>

    <!-- 错误态 -->
    <div v-if="error" class="state-error">
      <span>{{ error }}</span>
      <el-button size="small" type="primary" @click="loadDashboard">重试</el-button>
    </div>

    <!-- 空态: 一个项目都没有 -->
    <div v-else-if="isEmpty" class="state-empty">
      <div class="empty-icon">
        <el-icon><FolderOpened /></el-icon>
      </div>
      <div class="empty-text">还没有项目可分析</div>
      <el-button size="small" type="primary" @click="gotoCreateProject">
        去创建项目
      </el-button>
    </div>

    <!-- 半空态: 有项目但都没扫过 -->
    <div v-else-if="isUnScanned" class="state-empty">
      <div class="empty-icon danger">
        <el-icon><Lock /></el-icon>
      </div>
      <div class="empty-text">
        你有 <b>{{ data?.project_count }}</b> 个项目还没做过安全审计
      </div>
      <el-button size="small" type="primary" :icon="Lock" @click="gotoProjectList">
        开始扫描
      </el-button>
    </div>

    <!-- 正常数据 -->
    <div v-else-if="data" class="card-body">
      <!-- 左:评分 + 严重度 + 趋势火花图 -->
      <div class="left-col">
        <div class="big-score">
          <div class="score-num font-display" :style="{ color: scoreColor }">
            {{ scoreText }}
          </div>
          <div class="score-label">平均风险评分</div>
        </div>
        <div class="sev-row">
          <div class="sev-pill sev-severe">
            <span class="sev-num font-mono">{{ data.severe_issues_total }}</span>
            <span class="sev-label">严重</span>
          </div>
          <div class="sev-pill sev-high">
            <span class="sev-num font-mono">{{ data.high_issues_total }}</span>
            <span class="sev-label">高</span>
          </div>
          <div class="sev-pill sev-medium">
            <span class="sev-num font-mono">{{ data.medium_issues_total }}</span>
            <span class="sev-label">中</span>
          </div>
          <div class="sev-pill sev-low">
            <span class="sev-num font-mono">{{ data.low_issues_total }}</span>
            <span class="sev-label">低</span>
          </div>
        </div>

        <!-- 趋势火花图 -->
        <div v-if="data.trend.length >= 2" class="sparkline-wrap">
          <svg
            class="sparkline"
            viewBox="0 0 260 52"
            preserveAspectRatio="none"
            aria-label="安全趋势火花图"
          >
            <path
              :d="sparklineArea(data.trend, 260, 52, 4)"
              fill="rgba(220, 73, 97, 0.10)"
              stroke="none"
            />
            <polyline
              :points="sparklinePoints(data.trend, 260, 52, 4)"
              fill="none"
              stroke="#DC4961"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <div class="sparkline-label font-mono">
            严重 + 高危趋势 ({{ days }}d)
          </div>
        </div>

        <div class="meta-row font-mono">
          {{ data.scanned_project_count }} / {{ data.project_count }} 项目已扫描
          <span class="meta-issues">· {{ totalIssues }} 个安全问题</span>
        </div>
      </div>

      <!-- 中:OWASP 热点 -->
      <div class="mid-col">
        <div class="col-title">OWASP 热点</div>
        <ul v-if="data.owasp_hotspots.length" class="hotspot-list">
          <li
            v-for="(h, idx) in data.owasp_hotspots"
            :key="h.owasp"
            class="hotspot-item"
          >
            <span class="hot-rank font-mono">#{{ idx + 1 }}</span>
            <span class="hot-name">{{ h.owasp }}</span>
            <span class="hot-count font-mono">{{ h.count }}</span>
          </li>
        </ul>
        <div v-else class="col-empty">暂无 OWASP 命中</div>
      </div>

      <!-- 右:高风险项目 -->
      <div class="right-col">
        <div class="col-title">高风险项目</div>
        <ul v-if="data.top_risky_projects.length" class="risky-list">
          <li
            v-for="p in data.top_risky_projects"
            :key="p.project_id"
            class="risky-item"
            tabindex="0"
            role="button"
            @click="gotoProject(p.project_id)"
            @keyup.enter="gotoProject(p.project_id)"
          >
            <div class="risky-info">
              <div class="risky-name">{{ p.project_name }}</div>
              <div class="risky-sub">
                <span v-if="p.severe_issues > 0" class="r-sev">
                  <i class="risk-dot severe"></i>{{ p.severe_issues }} 严重
                </span>
                <span v-if="p.high_issues > 0" class="r-high">
                  <i class="risk-dot high"></i>{{ p.high_issues }} 高
                </span>
              </div>
            </div>
            <div class="risky-score font-mono">
              {{ p.risk_score === null ? '—' : p.risk_score }}
            </div>
          </li>
        </ul>
        <div v-else class="col-empty">所有项目都健康</div>
      </div>
    </div>
  </article>
</template>

<style scoped lang="scss">
.posture-card {
  background:
    linear-gradient(135deg, rgba(217, 59, 59, 0.035), rgba(91, 88, 232, 0.035)),
    var(--surface-1);
  border: var(--hairline);
  border-radius: 10px;
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.head-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.head-mark {
  box-shadow: 0 5px 14px -8px rgba(91, 88, 232, 0.7);
}

.head-title h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900);
}

.head-range {
  font-size: 11px;
  color: var(--gray-500);
  padding: 2px 8px;
  background: var(--gray-100);
  border-radius: 10px;
}

.card-link {
  font-size: 12px;
  color: #D93B3B;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 2px;

  &:hover { text-decoration: underline; }
}

.state-empty,
.state-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  gap: 8px;
}

.state-error {
  color: #DC4961;
  font-size: 13px;
}

.empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 10px;
  color: var(--brand-600);
  background: var(--brand-50);
  border: 1px solid var(--brand-100);
  font-size: 20px;

  &.danger {
    color: #D93B3B;
    background: rgba(217, 59, 59, 0.08);
    border-color: rgba(217, 59, 59, 0.18);
  }
}

.empty-text {
  font-size: 13px;
  color: var(--gray-600);
  text-align: center;

  b { color: var(--gray-900); }
}

.card-body {
  display: grid;
  grid-template-columns: 260px 1fr 1fr;
  gap: 18px;
  align-items: stretch;
}

@media (max-width: 900px) {
  .card-body {
    grid-template-columns: 1fr;
  }
}

/* ===== 左列 ===== */
.left-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.big-score {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.score-num {
  font-size: 38px;
  font-weight: 600;
  line-height: 1;
}

.score-label {
  font-size: 12px;
  color: var(--gray-600);
}

.sev-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
}

.sev-pill {
  background: #fff;
  border-radius: 6px;
  padding: 6px 4px;
  text-align: center;
}

.sev-num {
  display: block;
  font-size: 16px;
  font-weight: 600;
}

.sev-label {
  font-size: 10px;
  color: var(--gray-600);
}

.sev-severe .sev-num { color: #DC4961; }
.sev-high .sev-num { color: #E27C4A; }
.sev-medium .sev-num { color: #D9A857; }
.sev-low .sev-num { color: #4FB87A; }

.meta-row {
  font-size: 11px;
  color: var(--gray-500);
}

.meta-issues {
  color: var(--gray-600);
}

/* Sparkline 火花图 */
.sparkline-wrap {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.sparkline {
  width: 100%;
  height: 36px;
  display: block;
}

.sparkline-label {
  font-size: 9.5px;
  color: var(--gray-400);
  text-align: right;
}

/* ===== 中列 / 右列 共用 ===== */
.col-title {
  font-size: 11px;
  color: var(--gray-500);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 8px;
}

.col-empty {
  font-size: 12px;
  color: var(--gray-500);
  padding: 12px;
  background: var(--gray-50);
  border-radius: 6px;
  text-align: center;
}

/* OWASP 热点 */
.hotspot-list,
.risky-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.hotspot-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  background: rgba(255, 255, 255, 0.6);
  border-radius: 6px;
  font-size: 12px;

  .hot-rank {
    color: var(--gray-500);
    font-size: 11px;
  }

  .hot-name {
    color: var(--gray-800);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hot-count {
    color: #D93B3B;
    font-weight: 600;
  }
}

/* 高风险项目 */
.risky-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.6);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
  outline: none;

  &:hover,
  &:focus {
    background: #fff;
    box-shadow: 0 1px 4px rgba(220, 73, 97, 0.18);
  }

  .risky-info {
    min-width: 0;
  }

  .risky-name {
    font-size: 12.5px;
    font-weight: 500;
    color: var(--gray-900);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .risky-sub {
    font-size: 11px;
    color: var(--gray-600);
    display: flex;
    gap: 8px;
    margin-top: 2px;

    span {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
  }

  .risk-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    display: inline-block;

    &.severe { background: #DC4961; }
    &.high { background: #E27C4A; }
  }

  .risky-score {
    font-size: 16px;
    font-weight: 600;
    color: #DC4961;
    margin-left: 8px;
  }
}
</style>
