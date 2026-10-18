<template>
  <div class="report-detail-page">
    <!-- ============ 顶部操作栏（打印时隐藏）============ -->
    <header class="page-head no-print">
      <el-button link class="back-btn" @click="goBack(router, '/reports')">
        <el-icon><ArrowLeft /></el-icon>返回列表
      </el-button>
      <h1 class="font-display">审查报告</h1>
      <div class="head-actions">
        <el-button v-if="canViewReport" @click="onPrint">
          <el-icon><Printer /></el-icon>打印
        </el-button>
        <el-button
          v-if="canExport('word')"
          :loading="exportingFormat === 'word'"
          :disabled="exportingFormat !== null"
          data-testid="report-export-word"
          @click="downloadWord"
        >
          <el-icon><Document /></el-icon>导出 Word
        </el-button>
        <el-button
          v-if="canExport('pdf')"
          type="primary"
          :loading="exportingFormat === 'pdf'"
          :disabled="exportingFormat !== null"
          data-testid="report-export-pdf"
          @click="downloadPdf"
        >
          <el-icon><Download /></el-icon>导出 PDF
        </el-button>
      </div>
    </header>

    <el-alert v-if="loadError" data-testid="report-load-error" class="no-print" :title="loadError" type="warning" :closable="false" show-icon>
      <el-button :loading="loading" @click="loadReport">重试读取报告</el-button>
    </el-alert>
    <el-alert
      v-if="reportAccessRevoked"
      data-testid="report-access-revoked"
      class="no-print"
      title="当前账号已无权查看此报告，已清除报告内容。"
      type="warning"
      :closable="false"
      show-icon
    />
    <section v-if="report && canViewReport" class="card report-execution-status" data-testid="report-execution-status" role="note">
      <strong>执行状态：{{ reportExecutionStatusLabel }}</strong>
      <p v-if="reportExecutionError">执行原因：{{ reportExecutionError }}</p>
      <p>验证覆盖：{{ reportCoverageLabel }}<span v-if="reportCoverageReason">；{{ reportCoverageReason }}</span></p>
      <p v-if="reportCoverageDetails" data-testid="report-coverage-details">验证项：{{ reportCoverageDetails }}</p>
      <p v-if="coverageSummary">覆盖明细：{{ coverageSummary }}</p>
      <ul v-if="coverageEntries.length" class="coverage-details">
        <li v-for="(entry, index) in coverageEntries" :key="`${index}-${entry}`">{{ entry }}</li>
      </ul>
      <p v-if="reportDurationNote">耗时说明：{{ reportDurationNote }}</p>
    </section>
    <section v-if="securityReport && canViewReport" class="card security-report-snapshot" data-testid="security-report-snapshot">
      <header>
        <div>
          <strong>{{ securityReport.source === 'fullchain_audit' ? '全链路源码审计' : '安全审计补充报告' }}</strong>
          <span class="security-report-status">{{ securityExecutionLabel }}</span>
        </div>
        <el-button v-if="securityReport.report_md" size="small" @click="showSecurityReport = !showSecurityReport">
          {{ showSecurityReport ? '收起审计记录' : '展开审计记录' }}
        </el-button>
      </header>
      <p>评分：{{ securityReport.score_available === false ? '未评估' : `${securityReport.risk_score ?? '未提供'}/100` }}</p>
      <p>覆盖状态：{{ securityCoverageLabel }}。具体问题明细见下方问题清单；未扫描范围不计为安全通过。</p>
      <div v-if="showSecurityReport && securityReport.report_md" class="security-report-body report-markdown" v-html="securityReportHtml" />
    </section>
    <section v-if="domainReportHtml && canViewReport" class="card domain-report-body report-markdown" data-testid="domain-report-body" v-html="domainReportHtml" />
    <!-- ============ T15 报告操作工具栏(模板类型 / 生成 / 预览 / 导出)============ -->
    <section v-if="report && canViewReport" class="report-toolbar no-print">
      <div class="toolbar-left">
        <span class="toolbar-label font-mono">模板类型</span>
        <el-select v-model="templateType" size="small" style="width: 200px">
          <el-option
            v-for="opt in templateTypeOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
      </div>
      <div class="toolbar-right">
        <el-button-group>
          <el-button
            v-if="canExport('json')"
            size="small"
            :loading="generatingFormat === 'json'"
            @click="handleGenerate('json')"
          >生成 JSON</el-button>
          <el-button
            v-if="canExport('html')"
            size="small"
            :loading="generatingFormat === 'html'"
            @click="handleGenerate('html')"
          >生成 HTML</el-button>
          <el-button
            v-if="canExport('pdf')"
            size="small"
            :loading="generatingFormat === 'pdf'"
            @click="handleGenerate('pdf')"
          >生成 PDF</el-button>
          <el-button
            v-if="canExport('word')"
            size="small"
            :loading="generatingFormat === 'word'"
            @click="handleGenerate('word')"
          >生成 Word</el-button>
        </el-button-group>
        <el-button
          v-if="canViewReport && canExportIssueDetails && !loadError"
          ref="previewButtonRef"
          size="small"
          :loading="previewing"
          data-testid="report-preview-button"
          @click="handlePreview"
        >
          <el-icon><View /></el-icon>预览 HTML
        </el-button>
        <el-dropdown v-if="hasExportFormat" trigger="click" @command="handleExport">
          <el-button size="small" :loading="exportingFormat !== null" :disabled="exportingFormat !== null">
            <el-icon><Download /></el-icon>导出报告
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="canExport('json')" :command="'json'">JSON</el-dropdown-item>
              <el-dropdown-item v-if="canExport('html')" :command="'html'">HTML</el-dropdown-item>
              <el-dropdown-item v-if="canExport('pdf')" :command="'pdf'">PDF</el-dropdown-item>
              <el-dropdown-item v-if="canExport('word')" :command="'word'">Word</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </section>

    <section v-if="isSandboxReport && canViewReport" class="card" role="note">
      <p>沙箱报告按“问题清单”的独立条目统计；条目数不代表已确认漏洞数，严重度仅采用报告明示标签。</p>
      <p v-if="report?.source?.report_issue_summary?.total == null">未保存可识别的问题清单，无法确定条目数；历史记录数保留在来源信息中。</p>
    </section>

    <section v-if="isSandboxReport && canViewReport" class="card report-coverage-note" data-testid="sandbox-report-coverage" role="note">
      <strong>验证范围：{{ sandboxCoverageLabel }}</strong>
      <p v-if="sandboxCoverage?.reason">{{ sandboxCoverage.reason }}</p>
      <p v-if="sandboxCoverageStatus !== 'complete'">评分仅表示本次已记录范围的结果，不代表完整黑盒/白盒验证通过。</p>
    </section>

    <section v-if="isSandboxReport && canViewReport" class="card report-coverage-note" data-testid="sandbox-ai-review-status" role="note">
      <strong>多 Agent AI 报告：{{ sandboxAiReviewLabel }}</strong>
      <p v-if="report?.source?.ai_review?.failure_reason">{{ report.source.ai_review.failure_reason }}</p>
      <p v-else-if="sandboxAiReviewStatus === 'unknown'">历史报告未保存 AI 审查状态，不能据此确认多 Agent AI 审查已完成。</p>
      <p v-else-if="sandboxAiReviewStatus === 'complete'">报告结构和 Agent 执行清单已通过完整性校验；其中模型发现仍按“未核实候选”展示。</p>
    </section>

    <section
      v-if="canViewReport && issuesLoadState === 'forbidden'"
      class="card report-issues-note"
      data-testid="report-issues-restricted"
      role="note"
    >
      当前账号没有问题明细查看权限。报告概览仍可查看；生成、预览和导出包含问题证据的代码审查报告需要该权限。
    </section>
    <section
      v-else-if="canViewReport && issuesLoadState === 'failed'"
      class="card report-issues-note"
      data-testid="report-issues-load-error"
      role="alert"
    >
      <span>问题明细暂时无法加载；报告概览仍可查看，明细统计未按空结果处理。</span>
      <el-button data-testid="report-issues-retry" size="small" :loading="issuesLoading" @click="loadIssues">重试读取明细</el-button>
    </section>

    <section
      v-if="canViewReport && issuesLoadState === 'loaded' && issueVerificationNotice"
      class="card report-coverage-note issue-verification-note"
      data-testid="report-issue-verification"
      role="note"
    >
      <strong>问题复核状态：{{ issueVerificationNotice }}</strong>
      <p>问题总数、严重度分布、Top 10 和评分按报告记录统计，包含待核实条目；源码引用匹配只证明引用可定位，不证明漏洞条件或影响已成立。</p>
    </section>

    <section v-if="canViewReport && exportErrorMessage" class="report-export-error no-print" role="alert">
      <strong>{{ exportErrorMessage }}</strong>
      <span v-if="exportErrorNextAction">{{ exportErrorNextAction }}</span>
      <el-button v-if="retryExportFormat && canExport(retryExportFormat)" size="small" @click="retryExport">重试导出</el-button>
    </section>

    <div v-if="report && canViewReport" class="report-paper">
      <!-- ============ 暗色封面 ============ -->
      <section class="cover">
        <header class="cover-head">
          <span class="prism-mark"></span>
          <div class="cover-id font-mono">PRISM REPORT · #{{ taskId }}</div>
          <div class="cover-date font-mono">{{ formatDate(report.task?.create_time as string) }}</div>
        </header>

        <div class="cover-main">
          <div class="cover-meta">
            <div class="cover-label font-mono">PROJECT</div>
            <h2 class="cover-project font-display">{{ projectName }}</h2>
            <p class="cover-task font-mono">{{ taskName }} · {{ reviewType }}</p>

            <div v-if="!isDomainReport" class="cover-tags">
              <span class="cover-tag">项目语言：{{ projectLanguage }}</span>
              <span v-if="reviewedLanguages" class="cover-tag">审查语言：{{ reviewedLanguages }}</span>
              <span class="cover-tag">{{ totalFiles }} 文件</span>
              <span class="cover-tag">{{ formatDuration(durationMs) }}</span>
              <span class="cover-tag">{{ ruleCount }} 规则</span>
              <span
                v-for="agent in agentReleases"
                :key="Number(agent.release_id)"
                class="cover-tag"
              >{{ agent.agent_name }} v{{ agent.agent_version }}</span>
            </div>
            <div v-else class="cover-tags" data-testid="domain-report-tags">
              <span class="cover-tag">{{ reviewType }}</span>
              <span class="cover-tag">覆盖状态：{{ reportCoverageLabel }}</span>
              <span class="cover-tag">报告条目：{{ totalIssues }}</span>
              <span class="cover-tag">{{ formatDuration(durationMs) }}</span>
            </div>
          </div>

          <div v-if="scoreAvailable" class="cover-score">
            <div class="score-ring">
              <svg viewBox="0 0 120 120" width="160" height="160">
                <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(255,255,255,.10)" stroke-width="6"/>
                <circle
                  cx="60" cy="60" r="52" fill="none"
                  :stroke="scoreFlatColor(score)" stroke-width="6"
                  stroke-linecap="round"
                  :stroke-dasharray="`${animatedScore * 3.26} 326`"
                  transform="rotate(-90 60 60)"
                />
              </svg>
              <div class="ring-text">
                <div class="ring-val font-display">{{ animatedScore }}</div>
            <div class="ring-out font-mono">{{ isDomainReport ? '测试评分' : '/ 100' }}</div>
              </div>
            </div>
            <div
              v-if="!isDomainReport"
              class="risk-tag"
              data-testid="report-risk-level"
              :style="{ color: scoreFlatColor(score), borderColor: scoreFlatColor(score) }"
            >
              {{ riskLevel }}
            </div>
            <div v-else class="risk-tag" data-testid="report-domain-score-label">
              领域评分
            </div>
          </div>
          <div v-else class="cover-score score-unavailable" data-testid="report-score-unavailable">
            <strong>未评估</strong>
            <span>本次报告没有有效评分，不能推断为零风险。</span>
          </div>
        </div>

        <p v-if="report.summary && !domainReportHtml" class="cover-summary">{{ summaryText }}</p>
      </section>

      <!-- ============ 严重度 + 修复进度 + 评分历史 ============ -->
      <section class="row-grid no-break">
        <div class="rg-card">
          <div class="rg-title font-display">严重度分布</div>
          <div class="sev-rows">
            <div
              v-for="r in severityRows"
              :key="r.key"
              class="sev-row"
              :title="`${r.label} ${r.value} 个 · 占比 ${displayPercent(r.percent)}%`"
            >
              <span class="sr-label">
                <span class="sr-dot" :style="{ background: r.color }"></span>{{ r.label }}
              </span>
              <div class="sr-bar">
                <div class="sr-fill" :style="{ width: `${r.percent}%`, background: r.color }"></div>
              </div>
              <span class="sr-val font-mono">{{ r.value }}<span class="sr-pct"> · {{ displayPercent(r.percent) }}%</span></span>
            </div>
          </div>
        </div>

        <div v-if="!isDomainReport" class="rg-card" data-testid="code-repair-progress">
          <div class="rg-title font-display">修复进度</div>
          <div class="fix-gauge">
            <svg viewBox="0 0 120 70" width="100%">
              <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="var(--gray-100)" stroke-width="10" stroke-linecap="round"/>
              <path
                d="M 10 60 A 50 50 0 0 1 110 60" fill="none"
                stroke="url(#fix-grad)" stroke-width="10" stroke-linecap="round"
                :stroke-dasharray="`${fixedPercent * 1.57} 1000`"
              />
              <defs>
                <linearGradient id="fix-grad" x1="0" x2="1">
                  <stop offset="0" stop-color="#5B58E8"/>
                  <stop offset="1" stop-color="#4FB87A"/>
                </linearGradient>
              </defs>
            </svg>
            <div class="fix-text">
              <span class="fix-val font-display">{{ fixedPercent }}%</span>
              <span class="fix-sub font-mono">已修复 {{ fixedCount }} / {{ totalIssues }}</span>
            </div>
          </div>
        </div>

        <div v-if="!isDomainReport" class="rg-card">
          <div class="rg-title font-display">代码规模</div>
          <div class="scale-rows">
            <div class="scale-row">
              <span class="font-mono scale-label">总文件</span>
              <span class="font-display scale-val">{{ totalFiles }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">{{ isSandboxReport ? '报告条目' : '问题' }}</span>
              <span class="font-display scale-val">{{ isSandboxReport && report?.source?.report_issue_summary?.total == null ? '—' : totalIssues }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">耗时</span>
              <span class="font-display scale-val">{{ formatDuration(durationMs) }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">规则</span>
              <span class="font-display scale-val">{{ ruleCount }}</span>
            </div>
          </div>
        </div>
        <div v-else class="rg-card" data-testid="domain-report-metrics">
          <div class="rg-title font-display">验证概况</div>
          <div class="scale-rows">
            <div class="scale-row">
              <span class="font-mono scale-label">验证覆盖</span>
              <span class="font-display scale-val">{{ reportCoverageLabel }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">报告条目</span>
              <span class="font-display scale-val">{{ totalIssues }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">耗时</span>
              <span class="font-display scale-val">{{ formatDuration(durationMs) }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ============ 8 维度雷达 ============ -->
      <section v-if="!isDomainReport" class="card no-break" data-testid="code-dimension-radar">
        <header class="card-head">
          <h3 class="font-display">8 维度评分雷达</h3>
          <p class="card-desc">代码规范 / 命名 / 注释 / 维护 / 性能 / 异常 / Bug / 安全</p>
        </header>
        <BaseChart v-if="hasDimData" :option="radarOption" height="360px" />
        <EmptyState v-else description="暂无 8 维度数据" compact />
      </section>

      <!-- ============ 文件列表 ============ -->
      <section v-if="!isDomainReport" class="card" data-testid="code-review-files">
        <header class="card-head">
          <h3 class="font-display">审查文件 ({{ report.files.length }})</h3>
        </header>
        <div class="paper-table-scroll" role="region" aria-label="审查文件表格，可横向滚动" tabindex="0">
          <table class="paper-table">
            <thead>
              <tr>
                <th>文件名</th>
                <th class="col-num">语言</th>
                <th class="col-num">问题</th>
                <th class="col-num">严重</th>
                <th class="col-num">评分</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(f, i) in report.files" :key="i">
                <td class="font-mono">{{ f.file_name }}</td>
                <td>{{ f.language || '-' }}</td>
                <td class="font-mono">{{ f.issue_count }}</td>
                <td class="font-mono" :style="{ color: f.severe_count ? 'var(--sev-severe)' : 'inherit' }">
                  {{ f.severe_count ?? 0 }}
                </td>
                <td class="font-mono" :style="{ color: scoreFlatColor(Number(f.score) || 0) }">
                  {{ f.score ?? '-' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ============ 使用规则 ============ -->
      <section v-if="report.rules_snapshot?.length" class="card">
        <header class="card-head">
          <h3 class="font-display">使用规则 ({{ report.rules_snapshot.length }})</h3>
        </header>
        <div class="rules-list">
          <span
            v-for="(rule, i) in report.rules_snapshot"
            :key="i"
            class="rule-chip font-mono"
          >
            {{ ruleLabel(rule) }}
          </span>
        </div>
      </section>

      <section
        v-if="canViewReport && issuesLoadState === 'loaded' && issues.length > 0"
        class="card report-issue-details"
        data-testid="report-issues-list"
        aria-labelledby="report-issues-heading"
      >
        <header class="card-head">
          <h3 id="report-issues-heading" class="font-display">问题明细（{{ issues.length }}）</h3>
          <p class="card-desc">逐条列出本报告记录的问题，包括未评分和待核实条目；严重度与复核状态沿用服务端记录。每页展示 {{ issueDisplayPageSize }} 条，避免长报告一次渲染过多内容。</p>
        </header>
        <div v-if="issues.length > issueDisplayPageSize" class="report-issues-navigation" data-testid="report-issues-navigation">
          <span>显示第 {{ issueDisplayRangeStart }}–{{ issueDisplayRangeEnd }} 条，共 {{ issues.length }} 条</span>
          <el-pagination
            data-testid="report-issues-pagination"
            layout="prev, pager, next"
            :pager-count="5"
            :current-page="issueDisplayPage"
            :page-size="issueDisplayPageSize"
            :total="issues.length"
            :disabled="issuesLoading"
            @current-change="setIssueDisplayPage"
          />
        </div>
        <ol class="report-issue-list">
          <li
            v-for="(issue, index) in displayedIssues"
            :key="issue.id"
            class="report-issue-item"
            :data-testid="`report-issue-item-${issue.id}`"
          >
            <article>
              <header class="report-issue-item-head">
                <div class="report-issue-title-group">
                  <span class="report-issue-index">问题 {{ issueDisplayRangeStart + index }}</span>
                  <h4>{{ issue.title || issue.issue_type || '未命名问题' }}</h4>
                </div>
                <div class="report-issue-badges">
                  <span class="report-issue-severity">{{ severityDisplayLabel(issue.severity) || '未标记严重度' }}</span>
                  <span class="report-issue-state">{{ issueStatusLabel(issue.status) }}</span>
                  <span class="report-issue-state">{{ deterministicCvssScore(issue) == null ? 'CVSS 未评分' : `CVSS ${deterministicCvssScore(issue)?.toFixed(1)}` }}</span>
                </div>
              </header>
              <dl class="report-issue-meta">
                <div><dt>类型</dt><dd>{{ issue.issue_type || '未分类' }}</dd></div>
                <div v-if="issue.file_name"><dt>位置</dt><dd class="font-mono">{{ issue.file_name }}{{ issue.line_number != null ? `:${issue.line_number}` : '' }}{{ issue.end_line != null && issue.end_line !== issue.line_number ? `–${issue.end_line}` : '' }}</dd></div>
                <div v-if="issue.cwe"><dt>CWE</dt><dd>{{ issue.cwe }}</dd></div>
                <div><dt>证据状态</dt><dd>{{ evidenceQualityLabel(issue.evidence_quality) }}</dd></div>
                <div><dt>人工复核</dt><dd>{{ issueReviewStatusLabel(issue) }}</dd></div>
              </dl>
              <div class="report-issue-content">
                <section class="report-issue-field">
                  <h5>问题说明</h5>
                  <div class="report-issue-markdown" v-html="renderIssueMarkdown(issue.description, '未提供问题说明')" />
                </section>
                <section v-if="issue.suggestion" class="report-issue-field">
                  <h5>建议</h5>
                  <div class="report-issue-markdown" v-html="renderIssueMarkdown(issue.suggestion)" />
                </section>
                <section v-if="issue.remediation" class="report-issue-field">
                  <h5>修复方案</h5>
                  <div class="report-issue-markdown" v-html="renderIssueMarkdown(issue.remediation)" />
                </section>
                <section v-if="issue.fixed_code" class="report-issue-field">
                  <h5>修复代码</h5>
                  <pre class="report-issue-code">{{ issue.fixed_code }}</pre>
                </section>
              </div>
            </article>
          </li>
        </ol>
      </section>

      <!-- ============ T15 v3 字段:CVSS 评分分布 ============ -->
      <section v-if="issues.length > 0" class="card v3-card no-break" v-loading="issuesLoading">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>CVSS 评分分布
          </h3>
          <p class="card-desc">基于 CVSS 3.x 基础评分的四档分布(共 {{ cvssTotal }} 个有评分的问题)</p>
        </header>
        <div v-if="cvssTotal > 0" class="cvss-rows">
          <div v-for="r in cvssRows" :key="r.key" class="cvss-row">
            <span class="cv-label">
              <span class="cv-dot" :style="{ background: r.color }"></span>{{ r.label }}
            </span>
            <div class="cv-bar">
              <div class="cv-fill" :style="{ width: `${r.percent}%`, background: r.color }"></div>
            </div>
            <span class="cv-val font-mono">{{ r.value }}</span>
          </div>
        </div>
        <EmptyState v-else description="CVSS 未评分" compact />
      </section>

      <!-- ============ T15 v3 字段:合规映射概览 ============ -->
      <section v-if="hasV3Data" class="card v3-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>合规映射概览
          </h3>
          <p class="card-desc">ISO 27001 / GDPR / PCI DSS / HIPAA 四大标准命中条目数</p>
        </header>
        <div v-if="hasCompliance" class="compliance-grid">
          <el-statistic title="ISO 27001" :value="complianceStats.iso27001" />
          <el-statistic title="GDPR" :value="complianceStats.gdpr" />
          <el-statistic title="PCI DSS" :value="complianceStats.pci_dss" />
          <el-statistic title="HIPAA" :value="complianceStats.hipaa" />
        </div>
        <EmptyState v-else description="暂无合规映射数据" compact />
      </section>

      <!-- ============ T15 v3 字段:Top 10 高危漏洞 ============ -->
      <section v-if="top10Issues.length > 0" class="card v3-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>Top 10 高危漏洞
          </h3>
          <p class="card-desc">按 CVSS 评分降序排列,点击行查看详细修复方案</p>
        </header>
        <div class="paper-table-scroll" role="region" aria-label="高危漏洞表格，可横向滚动" tabindex="0">
          <table class="paper-table top10-table">
            <thead>
              <tr>
                <th class="col-rank">#</th>
                <th>漏洞标题</th>
                <th class="col-num">CVSS</th>
                <th class="col-num">CWE</th>
                <th class="col-num">严重度</th>
                <th class="col-num">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(it, idx) in top10Issues"
                :key="it.id"
                :class="{ 'row-selected': selectedRemediationIssue?.id === it.id }"
                :aria-current="selectedRemediationIssue?.id === it.id ? 'true' : undefined"
                @click="selectRemediation(it.id)"
              >
                <td class="font-mono col-rank">{{ idx + 1 }}</td>
                <td class="issue-title">
                  <span class="it-name">{{ it.title || it.issue_type }}</span>
                  <span v-if="it.file_name" class="it-file font-mono">{{ it.file_name }}:{{ it.line_number ?? '?' }}</span>
                  <span
                    v-if="issueReviewStatusLabel(it)"
                    class="it-review-state"
                    :class="{ 'is-pending': isIssuePendingReview(it) }"
                    :data-testid="`issue-review-status-${it.id}`"
                  >{{ issueReviewStatusLabel(it) }}</span>
                </td>
                <td class="font-mono col-num" :style="{ color: cvssSeverityColor(deterministicCvssScore(it)), fontWeight: 600 }">
                  {{ deterministicCvssScore(it)?.toFixed(1) ?? '未评分' }}
                </td>
                <td class="font-mono col-num">{{ it.cwe || it.issue_type || '-' }}</td>
                <td class="col-num">
                  <span
                    class="sev-tag"
                    :style="{ color: cvssSeverityColor(deterministicCvssScore(it)), borderColor: cvssSeverityColor(deterministicCvssScore(it)) }"
                  >{{ cvssSeverityLabel(deterministicCvssScore(it)) }}</span>
                </td>
                <td class="col-num">
                  <el-button
                    link
                    type="primary"
                    size="small"
                    @click.stop="selectRemediation(it.id)"
                  >查看修复方案</el-button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ============ T15 v3 字段:详细修复方案 ============ -->
      <section
        v-if="selectedRemediationIssue"
        id="remediation-detail"
        ref="remediationSectionRef"
        class="card v3-card no-break remediation-detail"
        data-testid="remediation-detail"
        aria-labelledby="remediation-heading"
      >
        <header class="card-head">
          <h3
            id="remediation-heading"
            ref="remediationHeadingRef"
            class="font-display remediation-heading"
            data-testid="remediation-heading"
            tabindex="-1"
          >
            <span class="prism-mark sm"></span>详细修复方案
          </h3>
          <p class="card-desc">
            {{ selectedRemediationIssue.title || selectedRemediationIssue.issue_type }}
            <span v-if="deterministicCvssScore(selectedRemediationIssue) != null" class="card-desc-meta font-mono">
              · CVSS {{ deterministicCvssScore(selectedRemediationIssue)?.toFixed(1) }}
              <span v-if="selectedRemediationIssue.cvss_vector"> · {{ selectedRemediationIssue.cvss_vector }}</span>
            </span>
          </p>
        </header>
        <div class="remediation-block">
          <div v-if="selectedRemediationIssue.remediation" class="remediation-content">
            <pre>{{ selectedRemediationIssue.remediation }}</pre>
          </div>
          <div v-else class="remediation-empty">
            <EmptyState description="该漏洞暂无详细修复方案" compact />
          </div>
          <div v-if="selectedRemediationIssue.suggestion" class="remediation-suggestion">
            <div class="rs-label font-mono">建议</div>
            <pre>{{ selectedRemediationIssue.suggestion }}</pre>
          </div>
          <div v-if="selectedRemediationIssue.fixed_code" class="remediation-code">
            <div class="rs-label font-mono">修复代码</div>
            <pre>{{ selectedRemediationIssue.fixed_code }}</pre>
          </div>
        </div>
      </section>

      <!-- ============ AI 总结 ============ -->
      <section v-if="report.summary && !domainReportHtml" class="card ai-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>AI 智能体总结建议
          </h3>
        </header>
        <div class="ai-summary" v-html="summaryHtml"></div>
      </section>
      <footer class="paper-foot font-mono">
        PRISM {{ APP_DISPLAY_VERSION }} · 基于大模型智能体的代码质量审查管理系统 · 生成于 {{ formatDate(new Date().toISOString()) }}
      </footer>
    </div>

    <PrismLoading
      v-else-if="loading"
      label="正在加载审查报告"
      sublabel="正在整理评分、问题和导出信息"
    />
    <EmptyState v-else-if="!loading && !loadError" description="尚未加载报告数据" />

    <el-dialog
      v-model="previewFallbackVisible"
      title="HTML 报告预览"
      width="min(1100px, 94vw)"
      top="5vh"
      append-to-body
      destroy-on-close
      class="report-preview-dialog no-print"
      @closed="handlePreviewFallbackClosed"
    >
      <iframe
        v-if="previewFallbackHtml"
        class="report-preview-frame"
        data-testid="report-preview-fallback"
        title="HTML 报告预览内容"
        :srcdoc="previewFallbackHtml"
        sandbox=""
        referrerpolicy="no-referrer"
      ></iframe>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { useAgentPageRefresh } from '@/composables/useAgentPageRefresh'
import { ref, computed, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

import { ArrowLeft, ArrowDown, Document, Download, Printer, View } from '@element-plus/icons-vue'
import { formatDateTime, parseUtcTimestamp } from '@/utils/format'
import type { EChartsCoreOption as EChartsOption } from 'echarts/core'
import BaseChart from '@/components/chart/BaseChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { mustDiscardReadSnapshot, readableError } from '@/composables/withFeedback'
import PrismLoading from '@/components/common/PrismLoading.vue'
import {
  getReportDetail,
  generateReport as apiGenerateReport,
  previewReport as apiPreviewReport,
  exportReport as apiExportReport,
} from '@/api/report'
import { getTaskIssues } from '@/api/review'
import type { ReportDetailOut, ReportIssue, ReportFormat, ReportTemplateType } from '@/types/report'
import type { TaskCoverageOut } from '@/types/review'
import { PRISM_SEVERITY_COLORS, PRISM_DIM_COLORS } from '@/components/chart/prismTheme'
import { reviewTypeLabel } from '@/constants/reviewType'
import { APP_DISPLAY_VERSION } from '@/constants/buildInfo'
import { goBack } from '@/utils/navigation'
import { renderMarkdown, stripMarkdown } from '@/utils/markdown'
import { reviewRiskLevel, reviewScoreColor } from '@/utils/reviewScore'
import { severityDisplayLabel } from '@/constants/severity'
import { useCountUp } from '@/composables/useCountUp'
import { taskDisplayTitle } from '@/utils/taskDisplayTitle'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const taskId = Number(route.params.id)

const loading = ref(true)
const loadError = ref('')
let reportRequestVersion = 0
let issueRequestVersion = 0
let accessGeneration = 0
let disposed = false
const report = ref<ReportDetailOut | null>(null)
const reportAccessRevoked = ref(false)
const selectedRemediationId = ref<number | null>(null)

// ===== T15 新增:报告生成 / 预览 / 导出状态 =====
/** 当前选择的模板类型(simple/detailed/compliance),影响生成/预览/导出 */
const templateType = ref<ReportTemplateType>('detailed')
/** 当前正在生成的格式(用于按钮 loading 态),null 表示无操作 */
const generatingFormat = ref<ReportFormat | null>(null)
/** 预览 HTML 报告的 loading 状态 */
const previewing = ref(false)
/** 弹窗被拦截时的页内安全预览。 */
const previewFallbackVisible = ref(false)
const previewFallbackHtml = ref('')
let pendingPreviewWindow: Window | null = null
type PreviewButtonTarget = { $el?: HTMLElement; focus?: () => void }
const previewButtonRef = ref<PreviewButtonTarget | null>(null)
/** 统一管理新窗口 Blob URL，避免请求失败或页面卸载时泄漏。 */
const previewUrlTimers = new Map<string, ReturnType<typeof setTimeout>>()
/** 当前正在导出的格式(用于导出下拉按钮 loading 态),null 表示无操作 */
const exportingFormat = ref<ReportFormat | null>(null)
const exportErrorMessage = ref('')
const exportErrorNextAction = ref('')
const retryExportFormat = ref<ReportFormat | null>(null)
/** 报告关联的全部问题列表(含 v3 字段,用于 CVSS/合规/Top10/修复方案展示) */
const issues = ref<ReportIssue[]>([])
/** issues 是否正在加载 */
const issuesLoading = ref(false)
const issuesLoadState = ref<'loading' | 'loaded' | 'forbidden' | 'failed'>('loading')
const issueDisplayPage = ref(1)
const issueDisplayPageSize = 20
const displayedIssues = computed(() => {
  const start = (issueDisplayPage.value - 1) * issueDisplayPageSize
  return issues.value.slice(start, start + issueDisplayPageSize)
})
const issueDisplayRangeStart = computed(() => issues.value.length
  ? (issueDisplayPage.value - 1) * issueDisplayPageSize + 1
  : 0)
const issueDisplayRangeEnd = computed(() => Math.min(issueDisplayPage.value * issueDisplayPageSize, issues.value.length))
function setIssueDisplayPage(page: number): void {
  const lastPage = Math.max(1, Math.ceil(issues.value.length / issueDisplayPageSize))
  issueDisplayPage.value = Math.min(Math.max(1, page), lastPage)
}
function captureAccessScope(): () => boolean {
  const generation = accessGeneration
  const account = `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
  return () => !disposed && generation === accessGeneration
    && account === `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
}
const issueVerificationNotice = computed(() => {
  const pending = issues.value.filter(isIssuePendingReview).length
  const unrecorded = issues.value.filter(issue => !issue.human_review_status).length
  const parts = []
  if (pending) parts.push(`${pending} 项待人工核实`)
  if (unrecorded) parts.push(`${unrecorded} 项复核状态未记录`)
  return parts.join('；')
})

function isIssuePendingReview(issue: ReportIssue): boolean {
  return issue.human_review_status === 'pending' || issue.human_review_status === 'evidence_requested'
}

function issueReviewStatusLabel(issue: ReportIssue): string {
  const labels: Record<string, string> = {
    pending: '待人工核实',
    evidence_requested: '待补充证据',
    accepted: '人工已接受',
    rejected: '人工已驳回',
    not_required: '无需人工复核',
  }
  return issue.human_review_status
    ? labels[issue.human_review_status] || '复核状态未知'
    : '复核状态未记录'
}

function issueStatusLabel(status?: string | null): string {
  const labels: Record<string, string> = {
    open: '未修复', unfixed: '未修复', fixed: '已修复', ignored: '已忽略',
    pending_review: '待复查', pending: '待处理', closed: '已关闭',
  }
  return labels[String(status || '').toLowerCase()] || status || '状态未记录'
}

function evidenceQualityLabel(quality?: ReportIssue['evidence_quality']): string {
  const labels: Record<string, string> = {
    verified: '源码引用可定位',
    direct: '存在直接证据',
    inferred: '推断线索',
    unsupported: '暂无支持证据',
  }
  return quality ? labels[quality] || '证据状态未知' : '未记录'
}

function renderIssueMarkdown(value?: string | null, fallback = ''): string {
  const content = typeof value === 'string' ? value.trim() : ''
  return renderMarkdown(content || fallback)
}

/** 只有后端确认由有效 v3.1 向量计算的分数才可作为 CVSS 展示。 */
function deterministicCvssScore(issue: ReportIssue): number | undefined {
  const score = issue.cvss_score
  if (
    issue.cvss_source !== 'vector'
    || issue.cvss_version !== '3.1'
    || !issue.cvss_vector?.trim()
    || typeof score !== 'number'
    || !Number.isFinite(score)
    || score < 0
    || score > 10
  ) {
    return undefined
  }
  return score
}

/** 模板类型下拉选项 */
const templateTypeOptions: Array<{ label: string; value: ReportTemplateType }> = [
  { label: '简洁模板 (Simple)', value: 'simple' },
  { label: '详细模板 (Detailed)', value: 'detailed' },
  { label: '合规模板 (Compliance)', value: 'compliance' },
]

const stats = computed<Record<string, unknown>>(() => report.value?.stats ?? {})
const isDomainReport = computed(() => {
  const sourceType = report.value?.source?.type || report.value?.task?.review_type
  return sourceType === 'sandbox_test' || sourceType === 'pentest'
})
function canAccessSourceReport(candidate: ReportDetailOut | null): boolean {
  if (!candidate) return true
  const source = candidate.source
  const sourceType = String(source?.type || candidate.task?.review_type || '')
  if (source?.security_report || ['security_scan', 'fullchain_audit'].includes(sourceType)) {
    return userStore.hasPermission('security:view')
  }
  if (sourceType === 'pentest') return userStore.hasPermission('pentest:view')
  return true
}
const canViewSourceReport = computed(() => canAccessSourceReport(report.value))
const canViewReport = computed(() => !!report.value
  && userStore.hasPermission('report:view') && canViewSourceReport.value)
const canExportIssueDetails = computed(() => {
  const sourceType = String(report.value?.source?.type || report.value?.task?.review_type || '')
  return isDomainReport.value
    || ['security_scan', 'fullchain_audit'].includes(sourceType)
    || userStore.hasPermission('issue:view')
})
function canExport(format: ReportFormat): boolean {
  return canViewReport.value && canExportIssueDetails.value && !loadError.value
    && userStore.hasPermission(`report:export:${format}`)
}
const hasExportFormat = computed(() => (['json', 'html', 'pdf', 'word'] as ReportFormat[]).some(canExport))
const isSandboxReport = computed(() => (report.value?.source?.type || report.value?.task?.review_type) === 'sandbox_test')
const securityReport = computed(() => canViewSourceReport.value ? report.value?.source?.security_report : undefined)
const securityReportHtml = computed(() => securityReport.value?.report_md ? renderMarkdown(securityReport.value.report_md) : '')
const domainReportHtml = computed(() => {
  const markdown = report.value?.source?.domain_report_md
  return canViewReport.value && typeof markdown === 'string' && markdown.trim() ? renderMarkdown(markdown) : ''
})
const securityExecutionLabel = computed(() => {
  const labels: Record<string, string> = { success: '执行完成', partial: '部分完成', failed: '执行失败' }
  return labels[securityReport.value?.execution_status ?? ''] ?? '状态未知'
})
const securityCoverageLabel = computed(() => {
  const status = String(securityReport.value?.coverage?.coverage_status ?? securityReport.value?.coverage?.verification_status ?? 'unknown')
  return ({ complete: '完整', partial: '部分', failed: '失败', unknown: '未知' }[status] ?? '未知')
})
const showSecurityReport = ref(false)
const sandboxCoverage = computed(() => report.value?.task?.coverage as TaskCoverageOut | undefined)
const sandboxCoverageStatus = computed(() => sandboxCoverage.value?.verification_status
  ?? sandboxCoverage.value?.coverage_status ?? 'unknown')
const coverageStepLabel = (value?: string | null) => {
  const labels: Record<string, string> = {
    passed: '已通过', complete: '已完成', completed: '已完成', success: '已完成',
    partial: '部分完成', failed: '失败', skipped: '未执行', not_required: '未纳入本次验证',
    unknown: '未知', receipt_missing: '缺少有效回执', pending: '等待执行', running: '执行中',
  }
  const status = String(value || '').toLowerCase()
  return labels[status] || status || '未记录'
}
const reportCoverageDetails = computed(() => {
  const coverage = sandboxCoverage.value
  if (!coverage) return ''
  const modeLabels: Record<string, string> = {
    whitebox: '白盒', blackbox: '黑盒', combined: '黑白盒', deploy: '部署检查',
  }
  const details: string[] = []
  const mode = String(coverage.requested_mode || '').toLowerCase()
  if (mode) details.push(`请求模式：${modeLabels[mode] || mode}`)
  for (const [key, label] of [
    ['deterministic_status', '确定性检查'],
    ['ai_dynamic_status', 'AI 动态用例'],
    ['blackbox_status', '黑盒验证'],
    ['remote_blackbox_status', '远程黑盒验证'],
  ] as const) {
    const status = coverage[key]
    if (status) details.push(`${label}：${coverageStepLabel(status)}`)
  }
  return details.join('；')
})
const sandboxCoverageLabel = computed(() => {
  if (sandboxCoverageStatus.value === 'complete') {
    if (sandboxCoverage.value?.requested_mode === 'whitebox'
      && sandboxCoverage.value.blackbox_status === 'not_required'
      && sandboxCoverage.value.remote_blackbox_status === 'not_required') {
      return '白盒验证完成；未执行黑盒或远程黑盒测试'
    }
    return '本次请求的验证步骤已完成'
  }
  if (sandboxCoverageStatus.value === 'partial') return '验证范围不完整'
  if (sandboxCoverageStatus.value === 'failed') return '验证未通过'
  return '验证范围未知'
})
const reportExecutionStatusLabel = computed(() => {
  const status = String(report.value?.task?.status || '').toLowerCase()
  const labels: Record<string, string> = {
    success: '执行完成',
    failed: '执行失败',
    cancelled: '已取消',
    expired: '授权已过期',
    stopped: '已停止',
    running: '执行中',
    pending: '等待执行',
  }
  return labels[status] || '状态未知'
})
const reportExecutionError = computed(() => String(report.value?.task?.error_message || '').trim())
const reportDurationNote = computed(() => String(report.value?.source?.duration_note || '').trim())
const reportCoverageLabel = computed(() => {
  const coverage = report.value?.task?.coverage as TaskCoverageOut | undefined
  const status = String(coverage?.verification_status || coverage?.coverage_status || '').toLowerCase()
  if (status === 'complete' || status === 'completed' || status === 'success') {
    if (coverage?.requested_mode === 'whitebox'
      && coverage.blackbox_status === 'not_required'
      && coverage.remote_blackbox_status === 'not_required') {
      return '白盒验证完成；未执行黑盒或远程黑盒测试'
    }
  }
  const labels: Record<string, string> = {
    complete: '本次请求的验证步骤已完成',
    completed: '本次请求的验证步骤已完成',
    success: '本次请求的验证步骤已完成',
    partial: '部分完成',
    failed: '验证未通过',
    incomplete: '覆盖不完整',
    not_started: '未开始执行',
    not_applicable: '不适用',
    pending: '等待执行',
    unknown: '未知',
  }
  return labels[status] || '未记录'
})
const coverageEntryText = (item: unknown): string => {
  if (typeof item === 'string') return item.trim()
  if (!item || typeof item !== 'object') return String(item ?? '').trim()
  const value = item as Record<string, unknown>
  const primary = [value.label, value.file_name, value.file_path, value.name, value.path, value.phase, value.url]
    .map(item => String(item ?? '').trim()).filter(Boolean)
  const annotations = [value.reason, value.error, value.status, value.status_code]
    .map(item => String(item ?? '').trim()).filter(Boolean)
  const uniquePrimary = [...new Set(primary)]
  const uniqueAnnotations = [...new Set(annotations)]
  if (uniquePrimary.length && uniqueAnnotations.length) {
    return `${uniquePrimary.join('；')}（${uniqueAnnotations.join('；')}）`
  }
  return [...uniquePrimary, ...uniqueAnnotations].join('；')
}
const reportCoverageReason = computed(() => {
  const coverage = report.value?.task?.coverage as TaskCoverageOut | undefined
  return [coverage?.reason, coverage?.error].map(value => String(value || '').trim()).filter(Boolean).join('；')
})
const coverageSummary = computed(() => {
  const coverage = report.value?.task?.coverage as TaskCoverageOut | undefined
  if (!coverage) return ''
  const parts: string[] = []
  const completedFiles = coverage.completed_files ?? coverage.completed_file_count ?? coverage.included_file_count
  const totalFiles = coverage.total_files ?? coverage.total_file_count
  if (completedFiles !== undefined || totalFiles !== undefined) {
    parts.push(`文件处理 ${completedFiles ?? '未记录'}/${totalFiles ?? '未记录'}`)
  }
  if (coverage.completed_chunks !== undefined || coverage.total_chunks !== undefined) {
    parts.push(`代码片段处理 ${coverage.completed_chunks ?? '未记录'}/${coverage.total_chunks ?? '未记录'}`)
  }
  const tested = Array.isArray(coverage.tested) ? coverage.tested.length : 0
  const unscanned = Math.max(
    typeof coverage.unscanned_files === 'number' ? coverage.unscanned_files : 0,
    Array.isArray(coverage.unscanned) ? coverage.unscanned.length : 0,
    Array.isArray(coverage.unscanned_file_names) ? coverage.unscanned_file_names.length : 0,
    Array.isArray(coverage.unscanned_phases) ? coverage.unscanned_phases.length : 0,
  )
  const skipped = Math.max(
    Array.isArray(coverage.skipped_phases) ? coverage.skipped_phases.length : 0,
    Array.isArray(coverage.skipped_files) ? coverage.skipped_files.length : 0,
    Array.isArray(coverage.skipped) ? coverage.skipped.length : 0,
  )
  if (tested || coverage.tested !== undefined) parts.push(`已记录覆盖项 ${tested} 项`)
  if (unscanned || coverage.unscanned_files !== undefined || coverage.unscanned !== undefined) {
    parts.push(`未扫描 ${unscanned} 项`)
  }
  if (skipped || coverage.skipped !== undefined || coverage.skipped_phases !== undefined) parts.push(`跳过 ${skipped} 项`)
  if (Array.isArray(coverage.completed_phases)) parts.push(`已完成阶段 ${coverage.completed_phases.length} 项`)
  if (Array.isArray(coverage.phases)) parts.push(`阶段 ${coverage.phases.length} 项`)
  return parts.length ? `${parts.join('；')}。该统计仅反映回执中记录的范围。` : ''
})
const coverageEntries = computed(() => {
  const coverage = report.value?.task?.coverage as TaskCoverageOut | undefined
  if (!coverage) return []
  const result: string[] = []
  const coverageGroups: Array<[string, (unknown[] | undefined)[]]> = [
    ['已测试', [coverage.tested]],
    ['未扫描', [coverage.unscanned]],
    ['未扫描文件', [
      coverage.unscanned_file_names,
      Array.isArray(coverage.unscanned_files) ? coverage.unscanned_files : undefined,
    ]],
    ['未执行阶段', [coverage.unscanned_phases]],
    ['排除', [coverage.excluded]],
    ['排除文件', [coverage.excluded_files]],
    ['跳过阶段', [coverage.skipped_phases]],
    ['跳过文件', [coverage.skipped_files]],
    ['跳过', [coverage.skipped]],
    ['失败文件', [coverage.failed_files]],
    ['错误', [coverage.errors]],
    ['停止前未处理', [coverage.cancelled_files]],
    ['已完成阶段', [coverage.completed_phases]],
  ]
  for (const [label, groups] of coverageGroups) {
    for (const entries of groups) {
      if (Array.isArray(entries)) {
        result.push(...entries.map(coverageEntryText).filter(Boolean).map(value => `${label}：${value}`))
      }
    }
  }
  if (Array.isArray(coverage.phases)) {
    for (const phase of coverage.phases) {
      const status = String(phase.status || '').toLowerCase()
      if (['complete', 'completed', 'success', 'passed'].includes(status)) continue
      const name = coverageEntryText(phase)
      if (name) result.push(`阶段未完成：${name}${status ? `（${status}）` : ''}`)
    }
  }
  return [...new Set(result)]
})

// AI 总结:封面用纯文本(剥离 markdown 符号),AI 总结卡片用消毒后的 markdown 渲染
const summaryText = computed(() => stripMarkdown((report.value?.summary as string) ?? ''))
const summaryHtml = computed(() => renderMarkdown((report.value?.summary as string) ?? ''))

const projectName = computed(() => (report.value?.project?.project_name as string) ?? '未命名项目')
const taskName = computed(() => {
  const task = report.value?.task
  const name = [task?.task_name, task?.name]
    .find((value) => typeof value === 'string' && value.trim())
  return taskDisplayTitle(typeof name === 'string' ? name : undefined, `任务 #${taskId}`)
})
const reviewType  = computed(() => reviewTypeLabel(
  report.value?.task?.review_type as string,
  String((report.value?.task?.coverage as TaskCoverageOut | undefined)?.requested_mode || ''),
))
const projectLanguage = computed(() => String(report.value?.project?.language ?? '').trim() || '未标注')
const reviewedLanguages = computed(() => [...new Set(
  (report.value?.files ?? [])
    .map(file => String(file.language ?? '').trim().toLowerCase())
    .filter(Boolean),
)].join('、'))
const totalFiles  = computed(() => Number(report.value?.task?.total_files ?? 0))
const durationMs  = computed<number | null>(() => {
  const value = report.value?.task?.duration_ms
  return value == null || !Number.isFinite(Number(value)) ? null : Number(value)
})
const ruleCount   = computed(() => report.value?.rules_snapshot?.length ?? 0)
const agentReleases = computed<Array<Record<string, unknown>>>(() => (
  (report.value?.task?.agent_releases as Array<Record<string, unknown>>) ?? []
))

const score = computed(() => {
  const value = Number(stats.value.score ?? 0)
  return Number.isFinite(value) ? value : 0
})
const scoreAvailable = computed(() => (
  stats.value.score !== null
  && stats.value.score !== undefined
  && (report.value?.task?.coverage as TaskCoverageOut | undefined)?.score_available !== false
))
const sandboxAiReviewStatus = computed(() => report.value?.source?.ai_review?.status ?? 'unknown')
const sandboxAiReviewLabel = computed(() => {
  if (sandboxAiReviewStatus.value === 'complete') return '完成'
  if (sandboxAiReviewStatus.value === 'incomplete') return '未完成（已提供确定性报告）'
  return '状态未知'
})
const animatedScoreValue = useCountUp(score, 1500)
const animatedScore = computed(() => Math.round(animatedScoreValue.value))
const totalIssues  = computed(() => Number(stats.value.total_issues ?? 0))
const severeCount  = computed(() => Number(stats.value.severe ?? 0))
const highCount    = computed(() => Number(stats.value.high ?? 0))
const mediumCount  = computed(() => Number(stats.value.medium ?? 0))
const lowCount     = computed(() => Number(stats.value.low ?? 0))
const fixedCount   = computed(() => Number(stats.value.fixed ?? 0))
const fixedPercent = computed(() => totalIssues.value > 0 ? Math.round((fixedCount.value / totalIssues.value) * 100) : 0)
const displayPercent = (percent: number) => Number.isFinite(percent) ? String(Math.round(percent * 10) / 10) : '0'

const severityRows = computed(() => {
  const total = Math.max(1, totalIssues.value)
  const rows = [
    { key: 'severe', label: '危急', value: severeCount.value, percent: (severeCount.value / total) * 100, color: PRISM_SEVERITY_COLORS.severe },
    { key: 'high',   label: '高',   value: highCount.value,   percent: (highCount.value   / total) * 100, color: PRISM_SEVERITY_COLORS.high   },
    { key: 'medium', label: '中',   value: mediumCount.value, percent: (mediumCount.value / total) * 100, color: PRISM_SEVERITY_COLORS.medium },
    { key: 'low',    label: '低',   value: lowCount.value,    percent: (lowCount.value    / total) * 100, color: PRISM_SEVERITY_COLORS.low    },
  ]
  if (!isSandboxReport.value) return rows
  const unclassified = Number((stats.value.severity as Record<string, number>)?.['未分级'] ?? 0)
  if (unclassified) rows.push({ key: 'unclassified', label: '未分级', value: unclassified,
    percent: (unclassified / total) * 100, color: 'var(--gray-500)' })
  return rows.filter((row) => row.value > 0)
})

import { DIM_KEYS, DIM_LABELS, normalizeDimKey } from '@/constants/dim'
import { ElMessage } from 'element-plus/es/components/message/index'

const dimKeys = DIM_KEYS
const dimLabels = dimKeys.map((k) => DIM_LABELS[k])

const dimScores = computed<number[]>(() => {
  const rawMap: Record<string, number> = {}
  const dim = (stats.value.dim_scores as Record<string, number>) ?? {}
  // 兼容后端可能用 maintainability/bug 等同义键
  for (const [k, v] of Object.entries(dim)) {
    const norm = normalizeDimKey(k)
    if (norm && typeof v === 'number') rawMap[norm] = v
  }
  return dimKeys.map((k) => {
    const v = rawMap[k]
    // 缺失维度用 NaN, ECharts 雷达图会显示为缺口,不再用整体 score 兜底掩盖
    return typeof v === 'number' && v > 0 ? Math.round(v) : NaN
  })
})

const hasDimData = computed(() => dimScores.value.some((v) => !Number.isNaN(v) && v > 0))

const radarOption = computed<EChartsOption>(() => ({
  tooltip: {},
  radar: {
    indicator: dimLabels.map((name) => ({ name, max: 100 })),
    radius: '68%',
    splitArea: { areaStyle: { color: ['#FAFAFE', '#F7F8FA'] } },
    axisLine: { lineStyle: { color: '#E0E3EA' } },
    splitLine: { lineStyle: { color: '#E0E3EA' } },
    axisName: { color: '#383E4D', fontSize: 11.5 },
  },
  series: [{
    type: 'radar',
    name: '8 维度评分',
    data: [{
      value: dimScores.value,
      name: '本次评分',
      areaStyle: { color: 'rgba(91, 88, 232, 0.18)' },
      lineStyle: { color: '#5B58E8', width: 2 },
      itemStyle: { color: '#5B58E8' },
    }],
  }],
  color: PRISM_DIM_COLORS,
}))

const riskLevel = computed(() => !scoreAvailable.value ? '未评估' : isSandboxReport.value ? '安全风险未评定（测试得分不代表风险）' : reviewRiskLevel(score.value))

function formatDate(s?: string): string {
  if (!s) return '-'
  return formatDateTime(parseUtcTimestamp(s), 'YYYY-MM-DD HH:mm')
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '未可靠记录'
  if (!ms) return '0ms'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

function scoreFlatColor(score: number): string {
  return reviewScoreColor(score)
}

function ruleLabel(rule: Record<string, unknown>): string {
  return (rule.rule_name as string) || (rule.name as string)
    || (rule.rule_code as string) || (rule.code as string) || '—'
}

async function loadReport() {
  if (!userStore.hasPermission('report:view')) {
    clearReportForAccessRevocation()
    loadError.value = '当前账号没有报告查看权限，请返回列表。'
    return
  }
  const version = ++reportRequestVersion
  const account = `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
  loading.value = true
  try {
    const result = await getReportDetail(taskId)
    if (disposed || version !== reportRequestVersion) return
    if (account !== `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
      || !userStore.hasPermission('report:view')) return
    if (!canAccessSourceReport(result)) {
      clearReportForAccessRevocation()
      return
    }
    report.value = result
    reportAccessRevoked.value = false
    loadError.value = ''
  } catch (error) {
    if (disposed || version !== reportRequestVersion) return
    if (mustDiscardReadSnapshot(error)) clearReportForAccessRevocation()
    loadError.value = `报告读取失败：${readableError(error)}。${report.value ? '保留上次成功的只读快照；重新读取成功后恢复报告操作。' : '可重试读取报告，或返回列表核对。'}`
  } finally {
    if (!disposed && version === reportRequestVersion) loading.value = false
  }
}

function downloadBlob(response: Blob, filename: string) {
  const url = window.URL.createObjectURL(response)
  let link: HTMLAnchorElement | undefined
  try {
    link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
  } finally {
    link?.remove()
    window.URL.revokeObjectURL(url)
  }
}

async function downloadWord(): Promise<void> {
  await handleExport('word')
}

async function downloadPdf(): Promise<void> {
  await handleExport('pdf')
}

function onPrint() {
  if (!canViewReport.value) return
  window.print()
}

// ===== T15 新增:加载 issues(含 v3 字段)=====

/**
 * 加载审查任务关联的全部问题(含 CVSS / 合规映射 / 修复方案 v3 字段)。
 * 后端报告详情不含 issues,需单独调用 review API 获取。
 */
async function loadIssues(): Promise<void> {
  const requestVersion = ++issueRequestVersion
  const account = `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
  const requestIsCurrent = () => !disposed
    && requestVersion === issueRequestVersion
    && account === `${String(userStore.profile?.id ?? '')}:${String(userStore.token ?? '')}`
  issues.value = []
  issueDisplayPage.value = 1
  if (!canViewReport.value) {
    issuesLoadState.value = 'forbidden'
    return
  }
  issues.value = []
  if (isDomainReport.value) {
    issuesLoadState.value = 'loaded'
    return
  }
  if (!userStore.hasPermission('issue:view')) {
    issuesLoadState.value = 'forbidden'
    return
  }
  issuesLoading.value = true
  issuesLoadState.value = 'loading'
  try {
    // 报告明细覆盖所有状态，不能把已修复/已忽略的问题漏出报告；也不能把首 1000 条当成全量。
    const pageSize = 1000
    const firstPage = await getTaskIssues(taskId, { page: 1, page_size: pageSize, status: 'all' })
    if (!requestIsCurrent() || !userStore.hasPermission('issue:view') || !canViewReport.value) return
    const total = Number(firstPage.total)
    if (!Number.isSafeInteger(total) || total < 0) throw new Error('invalid issue total')
    const allIssues = [...((firstPage.items as unknown as ReportIssue[]) ?? [])]
    let page = 1
    while (allIssues.length < total) {
      page += 1
      const next = await getTaskIssues(taskId, { page, page_size: pageSize, status: 'all' })
      if (!requestIsCurrent() || !userStore.hasPermission('issue:view') || !canViewReport.value) return
      const items = (next.items as unknown as ReportIssue[]) ?? []
      if (items.length === 0 || next.total !== total) throw new Error('incomplete issue pages')
      allIssues.push(...items)
    }
    if (allIssues.length !== total) throw new Error('inconsistent issue total')
    issues.value = allIssues
    issueDisplayPage.value = 1
    issuesLoadState.value = 'loaded'
  } catch {
    if (!requestIsCurrent()) return
    // 主报告继续可见，但明细加载失败不能伪装为“零问题”。
    issues.value = []
    issueDisplayPage.value = 1
    issuesLoadState.value = 'failed'
  } finally {
    if (requestIsCurrent()) issuesLoading.value = false
  }
}

async function loadReportAndIssues(): Promise<void> {
  await loadReport()
  if (canViewReport.value) await loadIssues()
}

function clearReportForAccessRevocation(): void {
  reportAccessRevoked.value = true
  accessGeneration++
  reportRequestVersion++
  issueRequestVersion++
  report.value = null
  issues.value = []
  issueDisplayPage.value = 1
  issuesLoadState.value = 'forbidden'
  issuesLoading.value = false
  previewFallbackVisible.value = false
  previewFallbackHtml.value = ''
  exportErrorMessage.value = ''
  exportErrorNextAction.value = ''
  retryExportFormat.value = null
  generatingFormat.value = null
  exportingFormat.value = null
  previewing.value = false
  try { pendingPreviewWindow?.close() } catch { /* 权限撤销后立即关闭预览窗口 */ }
  pendingPreviewWindow = null
  loading.value = false
  loadError.value = ''
}

watch(() => [
  userStore.token,
  userStore.profile?.id,
  userStore.hasPermission('report:view'),
  userStore.hasPermission('issue:view'),
  userStore.hasPermission('security:view'),
  userStore.hasPermission('pentest:view'),
], (current, previous) => {
  if (disposed) return
  const accountChanged = current[0] !== previous[0] || current[1] !== previous[1]
  if (accountChanged) {
    accessGeneration++
    reportRequestVersion++
    issueRequestVersion++
    report.value = null
    issues.value = []
    issueDisplayPage.value = 1
    issuesLoadState.value = 'loading'
    issuesLoading.value = false
    loading.value = false
    reportAccessRevoked.value = false
    loadError.value = ''
    previewFallbackVisible.value = false
    previewFallbackHtml.value = ''
    exportErrorMessage.value = ''
    exportErrorNextAction.value = ''
    retryExportFormat.value = null
    generatingFormat.value = null
    exportingFormat.value = null
    previewing.value = false
    try { pendingPreviewWindow?.close() } catch { /* 切换账号后不保留旧账号预览 */ }
    pendingPreviewWindow = null
    if (userStore.token && userStore.profile && userStore.hasPermission('report:view')) {
      void loadReportAndIssues()
    } else {
      issuesLoadState.value = 'forbidden'
      loadError.value = '当前账号没有报告查看权限，请返回列表。'
    }
    return
  }

  if (!userStore.hasPermission('report:view') || !canAccessSourceReport(report.value)) {
    if (report.value || !userStore.hasPermission('report:view')) clearReportForAccessRevocation()
    return
  }
  if (reportAccessRevoked.value) {
    reportAccessRevoked.value = false
    void loadReportAndIssues()
    return
  }
  if (!userStore.hasPermission('issue:view') && !isDomainReport.value) {
    issueRequestVersion++
    issues.value = []
    issueDisplayPage.value = 1
    issuesLoading.value = false
    issuesLoadState.value = 'forbidden'
    selectedRemediationId.value = null
    return
  }
  if (userStore.hasPermission('issue:view') && canViewReport.value) void loadIssues()
}, { flush: 'sync' })

// ===== T15 新增:v3 字段计算属性 =====

/** CVSS 评分四档分布(0-3.9 低 / 4-6.9 中 / 7-8.9 高 / 9-10 危急) */
const cvssDistribution = computed(() => {
  const dist = { low: 0, medium: 0, high: 0, critical: 0 }
  for (const it of issues.value) {
    const s = deterministicCvssScore(it)
    if (s === undefined) continue
    if (s >= 9) dist.critical += 1
    else if (s >= 7) dist.high += 1
    else if (s >= 4) dist.medium += 1
    else dist.low += 1
  }
  return dist
})

/** CVSS 分布总数(仅统计有评分的问题) */
const cvssTotal = computed(() =>
  cvssDistribution.value.low + cvssDistribution.value.medium
  + cvssDistribution.value.high + cvssDistribution.value.critical,
)

/** CVSS 四档分布行(带颜色与百分比,用于进度条展示) */
const cvssRows = computed(() => {
  const total = Math.max(1, cvssTotal.value)
  return [
    {
      key: 'critical', label: '危急 (9.0-10.0)', value: cvssDistribution.value.critical,
      percent: (cvssDistribution.value.critical / total) * 100, color: '#DC4961',
    },
    {
      key: 'high', label: '高 (7.0-8.9)', value: cvssDistribution.value.high,
      percent: (cvssDistribution.value.high / total) * 100, color: '#E27C4A',
    },
    {
      key: 'medium', label: '中 (4.0-6.9)', value: cvssDistribution.value.medium,
      percent: (cvssDistribution.value.medium / total) * 100, color: '#D9A857',
    },
    {
      key: 'low', label: '低 (0-3.9)', value: cvssDistribution.value.low,
      percent: (cvssDistribution.value.low / total) * 100, color: '#4FB87A',
    },
  ]
})

/** 合规映射概览:四个标准各自的命中条目数 */
const complianceStats = computed(() => {
  const counts = { iso27001: 0, gdpr: 0, pci_dss: 0, hipaa: 0 }
  for (const it of issues.value) {
    const m = it.compliance_mapping
    if (!m) continue
    counts.iso27001 += Array.isArray(m.iso27001) ? m.iso27001.length : 0
    counts.gdpr += Array.isArray(m.gdpr) ? m.gdpr.length : 0
    counts.pci_dss += Array.isArray(m.pci_dss) ? m.pci_dss.length : 0
    counts.hipaa += Array.isArray(m.hipaa) ? m.hipaa.length : 0
  }
  return counts
})

/** 是否有任何合规映射数据 */
const hasCompliance = computed(() => {
  const s = complianceStats.value
  return s.iso27001 + s.gdpr + s.pci_dss + s.hipaa > 0
})

/** Top 10 高危漏洞列表(仅按可验证的向量派生评分排序) */
const top10Issues = computed<ReportIssue[]>(() => {
  return issues.value
    .filter((it) => deterministicCvssScore(it) !== undefined)
    .slice()
    .sort((a, b) => (deterministicCvssScore(b) ?? 0) - (deterministicCvssScore(a) ?? 0))
    .slice(0, 10)
})

/** 选中展示修复方案的 issue(默认 Top1,可点击切换) */
/** 当前展示修复方案的 issue 对象 */
const selectedRemediationIssue = computed<ReportIssue | null>(() => {
  if (!top10Issues.value.length) return null
  const id = selectedRemediationId.value
  if (id !== null) {
    return top10Issues.value.find((it) => it.id === id) ?? top10Issues.value[0]
  }
  return top10Issues.value[0]
})

/** 是否有 v3 字段数据(决定整个 v3 区块是否显示) */
const hasV3Data = computed(() => {
  return cvssTotal.value > 0 || hasCompliance.value || top10Issues.value.length > 0
})

// ===== T15 新增:报告生成 / 预览 / 导出方法 =====

/**
 * 根据 cvss_score 返回严重度标签文本。
 * @param score - CVSS 评分
 * @returns 严重度中文标签
 */
function cvssSeverityLabel(score?: number | null): string {
  if (typeof score !== 'number') return '未评分'
  if (score >= 9) return '危急'
  if (score >= 7) return '高'
  if (score >= 4) return '中'
  return '低'
}

/**
 * 根据 cvss_score 返回严重度颜色(与 cvssRows 颜色一致)。
 * @param score - CVSS 评分
 * @returns 颜色十六进制字符串
 */
function cvssSeverityColor(score?: number | null): string {
  if (typeof score !== 'number') return '#909399'
  if (score >= 9) return '#DC4961'
  if (score >= 7) return '#E27C4A'
  if (score >= 4) return '#D9A857'
  return '#4FB87A'
}

/**
 * 生成报告(JSON/HTML/PDF/Word)。
 * JSON/HTML 返回字符串(触发下载为文本文件),PDF/Word 返回 Blob 触发下载。
 * @param format - 报告格式
 */
async function handleGenerate(format: ReportFormat): Promise<void> {
  if (!canExport(format) || generatingFormat.value !== null) return
  const isCurrentScope = captureAccessScope()
  generatingFormat.value = format
  try {
    const result = await apiGenerateReport(taskId, format, templateType.value)
    if (!isCurrentScope() || !canViewReport.value || !canExport(format)) return
    if (result instanceof Blob) {
      // pdf / word
      const ext = format === 'pdf' ? 'pdf' : 'docx'
      downloadBlob(result, `review_report_${taskId}.${ext}`)
      ElMessage.success(`${format.toUpperCase()} 报告生成成功`)
    } else {
      // json / html 返回字符串
      const ext = format === 'json' ? 'json' : 'html'
      const blob = new Blob([result], {
        type: format === 'json' ? 'application/json' : 'text/html',
      })
      downloadBlob(blob, `review_report_${taskId}.${ext}`)
      ElMessage.success(`${format.toUpperCase()} 报告生成成功`)
    }
  } catch (error) {
    if (isCurrentScope()) showExportError(error, format, '生成')
  } finally {
    if (isCurrentScope()) generatingFormat.value = null
  }
}

/**
 * 释放预览 Blob URL 及对应计时器。
 */
function releasePreviewUrl(url: string): void {
  const timer = previewUrlTimers.get(url)
  if (timer) clearTimeout(timer)
  previewUrlTimers.delete(url)
  window.URL.revokeObjectURL(url)
}

/** 在新窗口完成加载后延迟释放 Blob URL。 */
function schedulePreviewUrlRelease(url: string): void {
  const timer = setTimeout(() => releasePreviewUrl(url), 60_000)
  previewUrlTimers.set(url, timer)
}

/** 清理页内预览内容，不在对话框关闭后长期保留报告 HTML。 */
function clearPreviewFallback(): void {
  previewFallbackHtml.value = ''
}

/** 关闭页内预览后把键盘焦点还给触发按钮。 */
async function handlePreviewFallbackClosed(): Promise<void> {
  clearPreviewFallback()
  await nextTick()
  const target = previewButtonRef.value
  const element = target?.$el ?? target
  element?.focus?.()
}

/** 同步预开与当前页面断开关系的窗口；安全属性设置失败时改走页内预览。 */
function preopenPreviewWindow(): Window | null {
  let candidate: Window | null
  try {
    candidate = window.open('about:blank', '_blank')
  } catch {
    return null
  }
  if (!candidate) return null

  try {
    candidate.opener = null
  } catch {
    try { candidate.close() } catch { /* 页内预览仍可继续 */ }
    return null
  }

  try {
    candidate.document.title = '正在准备 HTML 报告'
    candidate.document.body.textContent = '正在准备 HTML 报告…'
  } catch {
    // 部分浏览器在断开 opener 后不允许读写预开页，不影响后续导航。
  }
  return candidate
}

/**
 * 预览 HTML 报告。
 * 用户点击时同步预开窗口，请求完成后再导航到 Blob URL；
 * 弹窗被拦截或在等待期间被关闭时，改用 sandbox iframe 页内预览。
 */
async function handlePreview(): Promise<void> {
  if (!canViewReport.value || !canExportIssueDetails.value || loadError.value || previewing.value) return
  const isCurrentScope = captureAccessScope()
  const popup = preopenPreviewWindow()
  pendingPreviewWindow = popup

  previewing.value = true
  previewFallbackVisible.value = false
  try {
    const html = await apiPreviewReport(taskId, templateType.value)
    if (!isCurrentScope() || !canViewReport.value || !canExportIssueDetails.value) {
      try { popup?.close() } catch { /* 不再使用已失效预览窗口 */ }
      return
    }
    if (popup && !popup.closed) {
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
      const url = window.URL.createObjectURL(blob)
      try {
        popup.location.replace(url)
        schedulePreviewUrlRelease(url)
        ElMessage.success('HTML 报告已在新窗口打开')
        return
      } catch {
        releasePreviewUrl(url)
        try { popup.close() } catch { /* 页内预览仍可继续 */ }
      }
    }

    previewFallbackHtml.value = html
    previewFallbackVisible.value = true
    await nextTick()
    if (!isCurrentScope() || !canViewReport.value) {
      previewFallbackVisible.value = false
      previewFallbackHtml.value = ''
      return
    }
    ElMessage.success('HTML 报告已在当前页面打开')
  } catch (error) {
    if (popup && !popup.closed) {
      try { popup.close() } catch { /* 忽略浏览器关闭窗口限制 */ }
    }
    if (isCurrentScope()) {
      exportErrorMessage.value = `预览报告失败：${readableError(error)}`
      exportErrorNextAction.value = '请核对报告状态后手动重新预览。'
      retryExportFormat.value = null
      ElMessage.error('预览报告失败')
    }
  } finally {
    if (pendingPreviewWindow === popup) pendingPreviewWindow = null
    if (isCurrentScope()) previewing.value = false
  }
}

/**
 * 导出报告(调用 exportReport 接口下载文件)。
 * @param format - 导出格式
 */
async function handleExport(format: ReportFormat): Promise<void> {
  if (!canExport(format) || exportingFormat.value !== null) return
  const isCurrentScope = captureAccessScope()
  exportingFormat.value = format
  exportErrorMessage.value = ''
  exportErrorNextAction.value = ''
  retryExportFormat.value = null
  try {
    const blob = await apiExportReport(taskId, format, templateType.value)
    if (!isCurrentScope() || !canViewReport.value || !canExport(format)) return
    const extMap: Record<ReportFormat, string> = {
      json: 'json', html: 'html', pdf: 'pdf', word: 'docx',
    }
    downloadBlob(blob, `review_report_${taskId}.${extMap[format]}`)
    ElMessage.success(`${format.toUpperCase()} 报告导出成功`)
  } catch (error) {
    if (isCurrentScope()) showExportError(error, format, '导出')
  } finally {
    if (isCurrentScope()) exportingFormat.value = null
  }
}

function showExportError(error: unknown, format: ReportFormat, action: string): void {
  const payload = error as { message?: string; next_action?: string }
  exportErrorMessage.value = payload?.message || `${format.toUpperCase()} 报告${action}失败`
  exportErrorNextAction.value = payload?.next_action || '请检查报告状态后重试'
  retryExportFormat.value = payload?.next_action ? (isDomainReport.value ? 'json' : format) : null
  ElMessage.error(exportErrorMessage.value)
}

function retryExport(): void {
  if (retryExportFormat.value) void handleExport(retryExportFormat.value)
}

/**
 * 选中某个 issue 展示其修复方案。
 * @param id - issue ID
 */
const remediationSectionRef = ref<HTMLElement | null>(null)
const remediationHeadingRef = ref<HTMLElement | null>(null)

async function selectRemediation(id: number): Promise<void> {
  selectedRemediationId.value = id
  await nextTick()
  const section = remediationSectionRef.value
  const heading = remediationHeadingRef.value
  if (!section) return
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  section.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
  if (heading) {
    try {
      heading.focus({ preventScroll: true })
    } catch {
      heading.focus()
    }
  }
}

onMounted(async () => {
  const shouldGenerate = route.query.generate === '1'
  if (shouldGenerate) void router.replace({ query: {} })
  await loadReportAndIssues()
  // 报告列表"生成报告"按钮跳转携带 ?generate=1,此处消费该 query 自动触发生成
  if (shouldGenerate && canExport('html')) {
    await handleGenerate('html')
  }
})

onBeforeUnmount(() => {
  disposed = true
  accessGeneration++
  reportRequestVersion++
  try { pendingPreviewWindow?.close() } catch { /* 浏览器可能已关闭该窗口 */ }
  pendingPreviewWindow = null
  for (const url of [...previewUrlTimers.keys()]) releasePreviewUrl(url)
})

useAgentPageRefresh({ pages: ['/reports', '/reviews', '/issues'], refresh: loadReportAndIssues })
</script>

<style scoped lang="scss">
.report-detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
  max-width: 100%;
}

.report-paper,
.domain-report-body,
.security-report-snapshot {
  min-width: 0;
  max-width: 100%;
}

.report-markdown {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;

  :deep(table) {
    width: 100%;
    max-width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
  }

  :deep(th),
  :deep(td) {
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  :deep(pre) {
    max-width: 100%;
    overflow: auto;
  }

  :deep(code) {
    overflow-wrap: anywhere;
  }
}

.report-execution-status {
  border-left: 4px solid var(--color-warning, #d99b22);
}

.report-execution-status p {
  margin: 6px 0 0;
  color: var(--gray-600, #586174);
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.report-coverage-note,
.report-issues-note {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.report-coverage-note p,
.report-issues-note p {
  margin: 0;
}

.issue-verification-note {
  border-color: var(--warning-200, #f0d9a5);
  background: var(--warning-50, #fffaf0);
}

/* ============ 顶部 ============ */
.page-head {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;

  h1 {
    margin: 0;
    flex: 1 1 180px;
    min-width: 0;
    font-size: 22px;
    font-weight: 600;
    color: var(--gray-900);
  }
}

.head-actions {
  display: flex;
  gap: 8px;
  flex: 0 1 560px;
  min-width: 0;
  flex-wrap: wrap;
  justify-content: flex-end;
}

/* ============ 报告纸 ============ */
.report-paper {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 16px;
  overflow: hidden;
  box-shadow: var(--shadow-2);
  display: flex;
  flex-direction: column;
}

/* ============ 暗色封面 ============ */
.cover {
  position: relative;
  background: linear-gradient(135deg, #161A24 0%, #1F1A3A 55%, #2D2B82 100%);
  color: #fff;
  padding: 40px 48px;
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    right: -160px;
    top: -180px;
    width: 540px;
    height: 540px;
    background: conic-gradient(from 220deg,
      var(--dim-style), var(--dim-naming), var(--dim-comment), var(--dim-maintain),
      var(--dim-perf), var(--dim-except), var(--dim-bug), var(--dim-security), var(--dim-style));
    filter: blur(80px);
    opacity: 0.42;
    border-radius: 50%;
    pointer-events: none;
  }

  & > * { position: relative; z-index: 1; }
}

.cover-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 24px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.cover-id {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--brand-300);
}

.cover-date {
  margin-left: auto;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
}

.cover-main {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 48px;
  align-items: center;
  margin: 32px 0;
}

.cover-label {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--brand-300);
  text-transform: uppercase;
}

.cover-project {
  font-size: 40px;
  font-weight: 600;
  letter-spacing: 0;
  line-height: 1.1;
  margin: 12px 0 8px;
  color: #fff;
}

.cover-task {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.65);
  margin: 0;
}

.cover-tags {
  display: flex;
  gap: 8px;
  margin-top: 20px;
  flex-wrap: wrap;
}

.cover-tag {
  padding: 4px 12px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  font-size: 11.5px;
  font-family: var(--font-mono);
  color: rgba(255, 255, 255, 0.75);
}

.cover-score {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
}

.score-ring {
  position: relative;
  width: 160px;
  height: 160px;
}

.ring-text {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.ring-val {
  font-size: 56px;
  font-weight: 600;
  letter-spacing: 0;
  line-height: 1;
}

.ring-out {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  margin-top: 4px;
}

.risk-tag {
  padding: 4px 14px;
  border: 1.5px solid;
  border-radius: 999px;
  font-size: 12px;
  font-family: var(--font-display);
  font-weight: 600;
}

.cover-summary {
  font-size: 13.5px;
  line-height: 1.85;
  color: rgba(255, 255, 255, 0.78);
  max-width: 720px;
  padding-top: 24px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  margin: 0;
}

/* ============ 多列卡片行 ============ */
.row-grid {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr;
  gap: 14px;
  padding: 24px;
  border-bottom: 1px solid var(--gray-100);
}

@media (max-width: 1100px) {
  .row-grid { grid-template-columns: 1fr; }
}

.rg-card {
  padding: 18px;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  background: #fff;
}

.rg-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 12px;
}

.sev-rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sev-row {
  display: grid;
  grid-template-columns: 70px 1fr 40px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.sr-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-700);
}

.sr-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.sr-bar {
  height: 8px;
  background: var(--gray-100);
  border-radius: 4px;
  overflow: hidden;
}

.sr-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.6s ease;
}

.sr-val {
  text-align: right;
  font-weight: 600;
  color: var(--gray-800);
}

.sr-pct {
  font-weight: 400;
  color: var(--gray-500);
  font-size: 11px;
}

.fix-gauge {
  position: relative;
  text-align: center;
}

.fix-text {
  margin-top: -16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.fix-val {
  font-size: 32px;
  font-weight: 600;
  color: var(--gray-900);
  line-height: 1;
}

.fix-sub {
  font-size: 11px;
  color: var(--gray-500);
}

.scale-rows {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 18px;
}

.scale-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scale-label {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.scale-val {
  font-size: 22px;
  font-weight: 600;
  color: var(--gray-900);
  letter-spacing: 0;
}

/* ============ 通用卡片 ============ */
.card {
  padding: 24px 28px;
  border-bottom: 1px solid var(--gray-100);

  &:last-of-type { border-bottom: none; }
}

.card-head {
  margin-bottom: 14px;

  h3 {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-900);
  }
}

.card-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--gray-500);
}

.report-issue-list {
  display: grid;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.report-issues-navigation {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  flex-wrap: wrap;
  margin-bottom: 14px;
  color: var(--gray-600);
  font-size: 12px;

  :deep(.el-pagination) { margin-left: auto; }
}

.report-issue-item {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--gray-100);
  border-radius: 10px;
  background: #fff;
}

.report-issue-item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.report-issue-title-group {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 10px;

  h4 {
    margin: 0;
    color: var(--gray-900);
    font-size: 15px;
    line-height: 1.55;
    overflow-wrap: anywhere;
  }
}

.report-issue-index {
  flex: none;
  color: var(--gray-500);
  font-size: 11px;
}

.report-issue-badges {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.report-issue-severity,
.report-issue-state {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 9px;
  border: 1px solid var(--gray-200);
  border-radius: 999px;
  color: var(--gray-700);
  background: var(--gray-50);
  font-size: 11px;
  line-height: 1.4;
}

.report-issue-severity {
  border-color: #e8c5c5;
  color: #a63b42;
  background: #fff8f7;
}

.report-issue-meta {
  display: flex;
  gap: 8px 18px;
  flex-wrap: wrap;
  margin: 12px 0 0;
  padding: 10px 0;
  border-top: 1px solid var(--gray-100);
  border-bottom: 1px solid var(--gray-100);

  div { display: flex; gap: 6px; min-width: 0; }
  dt { color: var(--gray-500); }
  dd { margin: 0; color: var(--gray-800); overflow-wrap: anywhere; }
}

.report-issue-content {
  display: grid;
  gap: 12px;
  margin-top: 14px;
}

.report-issue-field {
  min-width: 0;

  h5 {
    margin: 0 0 5px;
    color: var(--gray-600);
    font-size: 12px;
    font-weight: 600;
  }
}

.report-issue-markdown {
  min-width: 0;
  color: var(--gray-800);
  font-size: 13px;
  line-height: 1.75;
  overflow-wrap: anywhere;

  :deep(p) { margin: 0 0 8px; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(ul), :deep(ol) { padding-left: 20px; margin: 4px 0; }
  :deep(li) { margin: 3px 0; }
  :deep(code) { padding: 1px 5px; border-radius: 4px; background: var(--gray-100); }
  :deep(pre) { max-width: 100%; overflow: auto; white-space: pre-wrap; word-break: break-word; }
}

.report-issue-code {
  max-width: 100%;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  background: var(--gray-50);
  color: var(--gray-800);
  font-family: var(--font-mono, 'SFMono-Regular', Consolas, monospace);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .report-issue-item { padding: 14px; }
  .report-issue-title-group { align-items: flex-start; flex-direction: column; gap: 2px; }
  .report-issues-navigation { align-items: flex-start; flex-direction: column; }
  .report-issues-navigation :deep(.el-pagination) { margin-left: 0; }
}

/* ============ 表格 ============ */
.paper-table-scroll {
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}

.paper-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;

  th, td {
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--gray-100);
  }

  th {
    background: var(--gray-50);
    color: var(--gray-600);
    font-weight: 500;
    font-size: 11.5px;
    letter-spacing: 0.02em;
  }

  td { color: var(--gray-700); }

  .col-num { text-align: right; width: 80px; }
}

@media (max-width: 600px) {
  .page-head {
    flex-wrap: wrap;

    h1 { min-width: 0; }
  }

  .head-actions {
    flex-basis: 100%;
    flex-wrap: wrap;

    :deep(.el-button) {
      flex: 1 1 100px;
      margin-left: 0;
    }
  }

  .cover { padding: 24px 18px; }

  .cover-head {
    flex-wrap: wrap;
    gap: 8px 12px;
  }

  .cover-id, .cover-date { overflow-wrap: anywhere; }

  .cover-main {
    grid-template-columns: minmax(0, 1fr);
    gap: 22px;
    margin: 24px 0;
  }

  .cover-meta { min-width: 0; }

  .cover-project {
    font-size: clamp(28px, 8vw, 34px);
    overflow-wrap: anywhere;
  }

  .cover-task, .cover-tag { overflow-wrap: anywhere; }

  .cover-score {
    flex-direction: row;
    justify-self: start;
    flex-wrap: wrap;
  }

  .score-ring, .score-ring svg {
    width: 120px;
    height: 120px;
  }

  .ring-val { font-size: 42px; }
  .card { padding: 20px 16px; }

  .paper-table { min-width: 580px; }
  .top10-table { min-width: 760px; }
}

/* ============ 规则列表 ============ */
.rules-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rule-chip {
  padding: 4px 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 4px;
  font-size: 11px;
  color: var(--gray-700);
}

/* ============ AI 总结 ============ */
.ai-card {
  background: linear-gradient(135deg, #FAFAFE, #F0EFFE);
}

.ai-summary {
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--gray-800);

  :deep(p) { margin: 0 0 10px; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(h1), :deep(h2), :deep(h3), :deep(h4) {
    margin: 14px 0 8px;
    font-weight: 600;
    color: var(--gray-900);
  }
  :deep(h1), :deep(h2) { font-size: 16px; }
  :deep(h3), :deep(h4) { font-size: 14px; }
  :deep(ul), :deep(ol) { padding-left: 20px; margin: 6px 0; }
  :deep(li) { margin: 3px 0; }
  :deep(strong) { font-weight: 600; color: var(--gray-900); }
  :deep(code) {
    background: var(--gray-100);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 12px;
  }
  :deep(pre) {
    background: var(--gray-100);
    padding: 10px 12px;
    border-radius: 8px;
    overflow-x: auto;
    margin: 8px 0;
  }
  :deep(pre code) { background: transparent; padding: 0; }
}

/* ============ T15 报告操作工具栏 ============ */
.report-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  box-shadow: var(--shadow-1);
  flex-wrap: wrap;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toolbar-label {
  font-size: 12px;
  color: var(--gray-600);
  letter-spacing: 0.04em;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* ============ T15 v3 字段卡片 ============ */
.v3-card {
  background: linear-gradient(180deg, #FAFCFE 0%, #FFFFFF 100%);
}

.card-desc-meta {
  margin-left: 6px;
  font-size: 11.5px;
  color: var(--gray-500);
}

/* ============ CVSS 评分分布 ============ */
.cvss-rows {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cvss-row {
  display: grid;
  grid-template-columns: 140px 1fr 48px;
  align-items: center;
  gap: 12px;
  font-size: 12.5px;
}

.cv-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--gray-700);
}

.cv-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.cv-bar {
  height: 10px;
  background: var(--gray-100);
  border-radius: 5px;
  overflow: hidden;
}

.cv-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.6s ease;
}

.cv-val {
  text-align: right;
  font-weight: 600;
  color: var(--gray-800);
}

/* ============ 合规映射概览 ============ */
.compliance-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

@media (max-width: 900px) {
  .compliance-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

/* ============ Top 10 高危漏洞 ============ */
.top10-table {
  .col-rank {
    width: 48px;
    text-align: center;
    color: var(--gray-500);
  }

  .issue-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .it-name {
    font-weight: 500;
    color: var(--gray-800);
  }

  .it-file {
    font-size: 11px;
    color: var(--gray-500);
  }

  .it-review-state {
    width: fit-content;
    border: 1px solid var(--gray-200);
    border-radius: 999px;
    padding: 1px 7px;
    color: var(--gray-600);
    font-size: 11px;
    line-height: 1.5;
  }

  .it-review-state.is-pending {
    border-color: var(--warning-300, #eacb79);
    color: var(--warning-700, #8c6412);
    background: var(--warning-50, #fffaf0);
  }

  tbody tr {
    cursor: pointer;
    transition: background 0.2s;

    &:hover {
      background: var(--gray-50);
    }
  }

  .row-selected {
    background: rgba(91, 88, 232, 0.06) !important;
  }
}

.sev-tag {
  display: inline-block;
  padding: 2px 10px;
  border: 1px solid;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.6;
}

/* ============ 详细修复方案 ============ */
.remediation-detail {
  scroll-margin-top: 24px;
}

.remediation-heading:focus-visible {
  outline: 2px solid var(--brand-500);
  outline-offset: 4px;
  border-radius: 2px;
}

.remediation-block {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.remediation-content,
.remediation-suggestion,
.remediation-code {
  pre {
    margin: 8px 0 0;
    padding: 14px 16px;
    background: var(--gray-50);
    border: 1px solid var(--gray-100);
    border-radius: 8px;
    font-family: var(--font-mono, 'SFMono-Regular', Consolas, monospace);
    font-size: 12.5px;
    line-height: 1.7;
    color: var(--gray-800);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
  }
}

.remediation-content pre {
  background: #FFFBF5;
  border-color: #F5E8D0;
}

.remediation-code pre {
  background: #F6F8FA;
  border-color: var(--gray-100);
}

.rs-label {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.remediation-empty {
  padding: 12px 0;
}

:global(.report-preview-dialog .el-dialog__body) {
  padding: 0;
}

.report-preview-frame {
  display: block;
  width: 100%;
  height: min(72vh, 820px);
  border: 0;
  background: #fff;
}

/* ============ 页脚 ============ */
.paper-foot {
  padding: 20px 28px;
  font-size: 11px;
  color: var(--gray-400);
  letter-spacing: 0.05em;
  text-align: center;
  background: var(--gray-50);
}

/* ============ 打印样式 ============ */
@media print {
  .no-print { display: none !important; }

  .report-detail-page {
    display: block;
    background: #fff;
    padding: 0;
  }

  .report-paper {
    border: none;
    border-radius: 0;
    box-shadow: none;
    display: block;
    overflow: visible;
  }

  .cover {
    page-break-after: always;
  }

  .paper-table-scroll { overflow: visible; }

  .paper-table {
    min-width: 0;
    table-layout: fixed;
    font-size: 10px;

    th, td {
      padding: 6px 4px;
      overflow-wrap: anywhere;
    }

    .col-num { width: auto; }
  }

  .top10-table {
    min-width: 0;

    .col-rank { width: 24px; }

    /* 打印页没有交互，释放这一列给漏洞标题。 */
    th:last-child, td:last-child { display: none; }
  }

  .no-break {
    page-break-inside: avoid;
  }

  .card {
    page-break-inside: avoid;
  }

  :global(html:has(.report-detail-page)),
  :global(body:has(.report-detail-page)),
  :global(#app:has(.report-detail-page)) {
    height: auto !important;
    overflow: visible !important;
    background: #fff !important;
  }

  /* Element Plus 按钮等不打印 */
  .el-button { display: none; }
}

@page {
  size: A4;
  margin: 1.5cm;
}
</style>
