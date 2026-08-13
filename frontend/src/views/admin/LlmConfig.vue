<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { getLlmConfig, updateLlmConfig, testLlmConfig, type LlmConfig } from '@/api/llmConfig'
import { ElMessage } from 'element-plus/es/components/message/index'

const mode = ref<'deepseek' | 'custom'>('deepseek')
const cfg = ref<LlmConfig | null>(null)
const form = reactive({ base_url: '', model: '', api_key: '' })
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)

async function load() {
  loading.value = true
  try {
    const c = await getLlmConfig()
    cfg.value = c
    mode.value = c.source === 'global' ? 'custom' : 'deepseek'
    form.base_url = c.base_url
    form.model = c.model
    form.api_key = ''
  } finally {
    loading.value = false
  }
}

function buildPayload(active: boolean) {
  const payload: Record<string, unknown> = {
    provider: mode.value === 'custom' ? 'custom' : 'deepseek',
    base_url: form.base_url,
    model: form.model,
    active,
  }
  if (form.api_key.trim()) payload.api_key = form.api_key.trim()
  return payload
}

async function test() {
  testResult.value = null
  testing.value = true
  try {
    const r = await testLlmConfig({
      base_url: form.base_url, model: form.model,
      api_key: form.api_key.trim() || undefined,
    })
    testResult.value = { success: r.success, message: r.message }
    if (r.success) ElMessage.success(r.message)
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || '测试失败' }
  } finally {
    testing.value = false
  }
}

async function save() {
  if (mode.value === 'custom') {
    if (!form.base_url.trim() || !form.model.trim()) {
      ElMessage.warning('请填写端点与模型名称')
      return
    }
    if (!cfg.value?.is_set && !form.api_key.trim()) {
      ElMessage.warning('请填写 API Key')
      return
    }
  }
  saving.value = true
  try {
    // DeepSeek 模式 → 关闭全局覆盖(active=false);自定义 → 启用
    cfg.value = await updateLlmConfig(buildPayload(mode.value === 'custom'))
    form.api_key = ''
    ElMessage.success(mode.value === 'custom'
      ? '已切换为自定义模型,全平台生效'
      : '已切换为系统默认 DeepSeek')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="llm-page">
    <div class="page-header">
      <h2>大模型配置</h2>
      <p class="page-sub">选择全平台使用的大模型;用户自定义 API 配置仍优先于此全局设置</p>
    </div>

    <el-card shadow="never" class="form-card" v-loading="loading">
      <el-alert v-if="cfg"
        :title="cfg.source === 'global'
          ? `当前:自定义模型生效(${cfg.model || '未填模型'})`
          : '当前:系统默认 DeepSeek'"
        :type="cfg.source === 'global' ? 'success' : 'info'" :closable="false"
        style="margin-bottom: 20px" />

      <el-radio-group v-model="mode" class="mode-group">
        <el-radio-button label="deepseek">系统默认 DeepSeek</el-radio-button>
        <el-radio-button label="custom">自定义 (OpenAI 兼容)</el-radio-button>
      </el-radio-group>

      <template v-if="mode === 'custom'">
        <el-form label-width="120px" style="max-width: 680px; margin-top: 20px">
          <el-form-item label="端点 Base URL">
            <el-input v-model="form.base_url" placeholder="请输入 OpenAI 兼容 Base URL" />
          </el-form-item>
          <el-form-item label="模型名称">
            <el-input v-model="form.model" placeholder="请输入模型名称" />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="form.api_key" type="password" show-password
              :placeholder="cfg?.is_set ? `已配置(${cfg.api_key_masked},留空则不修改)` : '请输入 API Key'" />
          </el-form-item>
          <el-form-item v-if="testResult" label=" ">
            <el-tag :type="testResult.success ? 'success' : 'danger'" size="large">
              {{ testResult.message }}
            </el-tag>
          </el-form-item>
        </el-form>
      </template>
      <p v-else class="ds-hint">使用仓库 .env 中配置的系统默认 DeepSeek,无需额外填写。</p>

      <div class="actions">
        <el-button v-if="mode === 'custom'" :loading="testing" @click="test">测试连接</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并应用</el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.llm-page { padding: 4px; }
.page-header { margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.mode-group { margin-bottom: 4px; }
.ds-hint { color: var(--el-text-color-secondary); margin: 18px 0; font-size: 14px; }
.actions { margin-top: 12px; display: flex; gap: 10px; }
</style>
