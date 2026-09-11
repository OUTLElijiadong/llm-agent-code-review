<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import dayjs from 'dayjs'
import { CopyDocument, Delete, Plus, Refresh, Remove } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { deleteBetaCode, generateBetaCodes, listBetaCodes, revokeBetaCode } from '@/api/betaCode'
import type { BetaCodeStatus, BetaInviteCode } from '@/types/betaCode'

import EmptyState from '@/components/common/EmptyState.vue'

const loading = ref(false)
const generating = ref(false)
const rows = ref<BetaInviteCode[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref<BetaCodeStatus | ''>('')
const generatedCodes = ref<string[]>([])
const codesDialogVisible = ref(false)

const generateForm = reactive({ count: 1, expiry_days: 7, label: '' })

const statusMeta: Record<BetaCodeStatus, { label: string; type: 'success' | 'info' | 'danger' | 'warning' }> = {
  active: { label: '可使用', type: 'success' },
  used: { label: '已使用', type: 'info' },
  revoked: { label: '已撤销', type: 'danger' },
  expired: { label: '已过期', type: 'warning' },
}

const activeCount = computed(() => rows.value.filter((row) => row.status === 'active').length)

async function loadCodes(): Promise<void> {
  loading.value = true
  try {
    const data = await listBetaCodes({ status: status.value, page: page.value, page_size: pageSize.value })
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function handleGenerate(): Promise<void> {
  generating.value = true
  try {
    const result = await generateBetaCodes({
      count: generateForm.count,
      expiry_days: generateForm.expiry_days,
      label: generateForm.label.trim() || undefined,
    })
    generatedCodes.value = result.codes
    codesDialogVisible.value = true
    page.value = 1
    await loadCodes()
  } finally {
    generating.value = false
  }
}

async function copyText(value: string, successMessage = '已复制'): Promise<void> {
  await navigator.clipboard.writeText(value)
  ElMessage.success(successMessage)
}

async function handleRevoke(row: BetaInviteCode): Promise<void> {
  await ElMessageBox.confirm(`确认撤销 ${row.display_prefix}？撤销后无法恢复。`, '撤销内测码', {
    type: 'warning',
    confirmButtonText: '撤销',
    cancelButtonText: '取消',
  })
  await revokeBetaCode(row.id)
  ElMessage.success('内测码已撤销')
  await loadCodes()
}

async function handleDelete(row: BetaInviteCode): Promise<void> {
  await ElMessageBox.confirm(
    `确认删除内测码记录 ${row.display_prefix}？该操作从数据库物理移除记录，不可恢复。`,
    '删除内测码',
    {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    },
  )
  await deleteBetaCode(row.id)
  ElMessage.success('内测码记录已删除')
  await loadCodes()
}

function formatTime(value?: string): string {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'
}

function changeFilter(): void {
  page.value = 1
  loadCodes()
}

onMounted(loadCodes)
</script>

<template>
  <div class="beta-code-page">
    <header class="page-header">
      <div>
        <h2>内测码管理</h2>
        <p>生成一次性注册码并跟踪使用状态。明文只在生成后显示一次。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadCodes">刷新</el-button>
    </header>

    <section class="generator" aria-label="生成内测码">
      <div class="generator-title">
        <el-icon><Plus /></el-icon>
        <span>生成内测码</span>
      </div>
      <el-form :model="generateForm" inline label-position="top">
        <el-form-item label="数量">
          <el-input-number v-model="generateForm.count" :min="1" :max="100" controls-position="right" />
        </el-form-item>
        <el-form-item label="有效期（天）">
          <el-input-number v-model="generateForm.expiry_days" :min="1" :max="90" controls-position="right" />
        </el-form-item>
        <el-form-item label="备注" class="label-field">
          <el-input v-model="generateForm.label" maxlength="100" placeholder="例如：第一批内测成员" clearable />
        </el-form-item>
        <el-form-item label=" ">
          <el-button type="primary" :icon="Plus" :loading="generating" @click="handleGenerate">
            生成
          </el-button>
        </el-form-item>
      </el-form>
    </section>

    <section class="code-list" aria-label="内测码列表">
      <div class="list-toolbar">
        <div class="list-summary">共 {{ total }} 个，当前页可用 {{ activeCount }} 个</div>
        <el-segmented
          v-model="status"
          :options="[
            { label: '全部', value: '' },
            { label: '可使用', value: 'active' },
            { label: '已使用', value: 'used' },
            { label: '已过期', value: 'expired' },
            { label: '已撤销', value: 'revoked' },
          ]"
          @change="changeFilter"
        />
      </div>

      <div class="code-cards" v-loading="loading" role="list" data-testid="code-cards">
        <EmptyState v-if="!rows.length" compact description="暂无内测码" />
        <article
          v-for="row in rows"
          :key="row.id"
          class="code-card"
          :data-status="row.status"
          role="listitem"
        >
          <span class="bc-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="bc-main">
            <div class="bc-line1">
              <code class="bc-code" :title="row.display_prefix">{{ row.display_prefix }}</code>
              <el-tag :type="statusMeta[row.status as BetaCodeStatus].type" effect="plain" size="small">
                {{ statusMeta[row.status as BetaCodeStatus].label }}
              </el-tag>
            </div>
            <div class="bc-line2 font-mono">
              <span class="bc-remark" :title="row.label || ''">备注 {{ row.label || '-' }}</span>
              <span>有效期至 {{ formatTime(row.expires_at) }}</span>
              <span>{{ row.used_by ? `使用用户 #${row.used_by}` : '使用用户 -' }}</span>
              <span v-if="row.used_at">使用于 {{ formatTime(row.used_at) }}</span>
              <span>创建于 {{ formatTime(row.create_time) }}</span>
            </div>
          </div>
          <div class="bc-actions">
            <el-button
              v-if="row.status === 'active'"
              text
              type="danger"
              :icon="Remove"
              @click="handleRevoke(row)"
            >撤销</el-button>
            <el-button
              text
              type="danger"
              :icon="Delete"
              @click="handleDelete(row)"
            >删除</el-button>
          </div>
        </article>
      </div>

      <el-pagination
        v-if="total > pageSize"
        v-model:current-page="page"
        v-model:page-size="pageSize"
        class="pagination"
        layout="total, prev, pager, next"
        :total="total"
        @current-change="loadCodes"
      />
    </section>

    <el-dialog v-model="codesDialogVisible" title="内测码已生成" width="min(640px, 92vw)" destroy-on-close>
      <el-alert
        title="这些明文不会再次显示，请现在复制并通过安全渠道发送。"
        type="warning"
        :closable="false"
        show-icon
      />
      <div class="generated-list">
        <div v-for="code in generatedCodes" :key="code" class="generated-row">
          <code>{{ code }}</code>
          <el-button :icon="CopyDocument" circle title="复制内测码" @click="copyText(code)" />
        </div>
      </div>
      <template #footer>
        <el-button
          :icon="CopyDocument"
          @click="copyText(generatedCodes.join('\n'), '全部内测码已复制')"
        >复制全部</el-button>
        <el-button type="primary" @click="codesDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.beta-code-page { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; gap: 20px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.page-header h2 { margin: 0; font-size: 24px; color: var(--gray-900); }
.page-header p { margin: 6px 0 0; color: var(--gray-500); }
.generator { padding: 20px 22px 4px; border: 1px solid var(--gray-200); border-radius: 8px; background: #fff; }
.generator,
.code-list { min-width: 0; }
.generator-title { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; font-weight: 650; }
.label-field { flex: 1; min-width: 240px; }
.label-field :deep(.el-form-item__content) { width: 100%; }
.code-list { border-top: 1px solid var(--gray-200); background: #fff; }
.list-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 0; }
.list-summary { color: var(--gray-600); font-size: 14px; }

/* ── 内测码卡片列表(替代表格:码+状态为主行,备注/有效期/使用人/时间降级为次行) ── */
.code-cards { display: grid; gap: 10px; min-height: 120px; }
.code-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 14px; align-items: center;
  padding: 13px 16px 13px 12px; border-radius: 12px;
  background: #fff; border: 1px solid var(--gray-200);
  transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.code-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.bc-band { width: 4px; height: 38px; border-radius: 999px; }
.bc-band[data-status='active'] { background: #40a35f; }
.bc-band[data-status='used'] { background: var(--gray-300, #cfd4dc); }
.bc-band[data-status='revoked'] { background: var(--sev-severe, #dc4961); }
.bc-band[data-status='expired'] { background: #d9a857; }
.bc-main { display: grid; gap: 5px; min-width: 0; }
.bc-line1 { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.bc-code { font-size: 13.5px; font-weight: 650; letter-spacing: .02em; color: var(--gray-900); }
.bc-line2 { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--gray-500); }
.bc-remark { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bc-actions { display: flex; gap: 2px; flex-shrink: 0; }
@media (prefers-reduced-motion: reduce) {
  .code-card { transition: none; }
}
.pagination { justify-content: flex-end; padding-top: 18px; }
.generated-list { display: grid; gap: 8px; max-height: 360px; overflow-y: auto; margin-top: 16px; }
.generated-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 46px; padding: 6px 8px 6px 14px; border: 1px solid var(--gray-200); border-radius: 6px; background: var(--gray-50); }
.generated-row code { font-size: 14px; overflow-wrap: anywhere; }
.muted { color: var(--gray-400); }
@media (max-width: 760px) {
  .page-header, .list-toolbar { align-items: stretch; flex-direction: column; }
  .generator :deep(.el-form--inline) { display: grid; }
  .generator :deep(.el-form-item) { margin-right: 0; }
  .label-field { min-width: 0; }
  .list-toolbar :deep(.el-segmented) { overflow-x: auto; }
  .code-card { grid-template-columns: auto minmax(0, 1fr); }
  .bc-actions { grid-column: 2; justify-self: end; }
  .bc-remark { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
}
</style>
