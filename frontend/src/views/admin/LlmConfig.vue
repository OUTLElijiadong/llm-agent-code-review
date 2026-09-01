<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { Check, Connection, Download, Refresh } from '@element-plus/icons-vue'

import {
  fetchLlmModels,
  getLlmConfig,
  testLlmConfig,
  updateLlmConfig,
  type LlmConfig,
  type LlmDraft,
} from '@/api/llmConfig'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

type AlertType = 'success' | 'warning' | 'error' | 'info'

const PROVIDER_PRESETS: Record<string, { base_url: string; model: string }> = {
  deepseek: { base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
  openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o' },
  custom: { base_url: '', model: '' },
}

const cfg = ref<LlmConfig | null>(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const fetchingModels = ref(false)
const restoring = ref(false)
const loadError = ref('')
const modelOptions = ref<string[]>([])
const operation = ref<{
  type: AlertType
  title: string
  description: string
} | null>(null)

const form = reactive({
  provider: 'deepseek',
  base_url: 'https://api.deepseek.com',
  api_key: '',
  model: 'deepseek-chat',
  timeout_seconds: 60,
  max_retries: 2,
  temperature: 0.2,
  active: false,
})

const statusLabel = computed(() => {
  if (cfg.value?.source === 'global') return `已启用全局配置 · ${cfg.value.model}`
  return '使用系统默认配置'
})

const statusType = computed<AlertType>(() => (
  cfg.value?.source === 'global' ? 'success' : 'info'
))

const fallbackLabel = computed(() => {
  switch (cfg.value?.fallback_reason) {
    case 'credential_unavailable': return '已保存配置的凭据不可用，运行时已回退系统默认'
    case 'incomplete_config': return '已保存配置不完整，运行时已回退系统默认'
    case 'invalid_config': return '历史配置无法解析，运行时已回退系统默认'
    default: return ''
  }
})

function errorText(error: unknown, fallback: string): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const message = String((error as { message?: unknown }).message || '').trim()
    if (message) return message
  }
  return fallback
}

function endpointIdentity(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, '')
  try {
    const parsed = new URL(trimmed)
    parsed.pathname = parsed.pathname.replace(
      /\/(?:chat\/completions|models|responses)\/?$/i,
      '',
    ) || '/'
    parsed.hash = ''
    return parsed.toString().replace(/\/+$/, '')
  } catch {
    return trimmed
  }
}

function applyConfig(value: LlmConfig): void {
  cfg.value = value
  form.provider = value.provider || 'deepseek'
  form.base_url = value.base_url || PROVIDER_PRESETS.deepseek.base_url
  form.model = value.model || PROVIDER_PRESETS.deepseek.model
  form.timeout_seconds = value.timeout_seconds
  form.max_retries = value.max_retries
  form.temperature = value.temperature
  form.active = value.source === 'global'
  form.api_key = ''
  modelOptions.value = form.model ? [form.model] : []
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    applyConfig(await getLlmConfig())
  } catch (error) {
    loadError.value = errorText(error, '配置加载失败，当前表单仍可编辑')
  } finally {
    loading.value = false
  }
}

function draftPayload(): LlmDraft {
  return {
    provider: form.provider,
    base_url: form.base_url.trim(),
    model: form.model.trim(),
    api_key: form.api_key.trim() || undefined,
    timeout_seconds: form.timeout_seconds,
    max_retries: form.max_retries,
    temperature: form.temperature,
  }
}

function validateBaseAndModel(requireModel = true): boolean {
  if (!form.base_url.trim()) {
    ElMessage.warning('请填写 Base URL')
    return false
  }
  if (requireModel && !form.model.trim()) {
    ElMessage.warning('请选择或填写当前模型')
    return false
  }
  return true
}

async function pullModels(): Promise<void> {
  if (!validateBaseAndModel(false)) return
  fetchingModels.value = true
  operation.value = null
  try {
    const result = await fetchLlmModels(draftPayload())
    modelOptions.value = Array.from(new Set([
      ...result.models,
      form.model.trim(),
    ].filter(Boolean)))
    if (result.selected_model) form.model = result.selected_model
    operation.value = {
      type: result.success ? (result.fallback ? 'warning' : 'success') : 'error',
      title: result.message,
      description: result.next_action || `耗时 ${result.duration_ms}ms，尝试 ${result.attempts} 次`,
    }
  } catch (error) {
    operation.value = {
      type: 'error',
      title: errorText(error, '模型列表拉取失败'),
      description: '已保留当前模型和全部输入，可以修改配置后重试或继续手工填写模型。',
    }
  } finally {
    fetchingModels.value = false
  }
}

async function test(): Promise<void> {
  if (!validateBaseAndModel()) return
  testing.value = true
  operation.value = null
  try {
    const result = await testLlmConfig(draftPayload())
    operation.value = {
      type: result.success ? 'success' : 'error',
      title: result.message,
      description: result.success
        ? `模型 ${result.model || form.model} · ${result.duration_ms}ms · ${result.attempts} 次尝试`
        : result.next_action || '请修改配置后重试。',
    }
  } catch (error) {
    operation.value = {
      type: 'error',
      title: errorText(error, '连接测试失败'),
      description: '输入未丢失，可以检查配置后再次测试。',
    }
  } finally {
    testing.value = false
  }
}

function canReuseStoredKey(): boolean {
  if (!cfg.value?.is_set) return false
  return endpointIdentity(form.base_url) === endpointIdentity(cfg.value.base_url)
}

async function save(): Promise<void> {
  if (!validateBaseAndModel()) return
  if (form.active && !form.api_key.trim() && !canReuseStoredKey()) {
    ElMessage.warning('启用新的全局端点前，请填写该端点的 API Key')
    return
  }
  saving.value = true
  operation.value = null
  try {
    const payload = { ...draftPayload(), active: form.active }
    cfg.value = await updateLlmConfig(payload)
    applyConfig(cfg.value)
    operation.value = {
      type: cfg.value.source === 'global' ? 'success' : 'info',
      title: cfg.value.source === 'global' ? '配置已保存并全局生效' : '配置已保存，继续使用系统默认',
      description: 'API Key 已安全处理，不会在页面回显。',
    }
    ElMessage.success('配置保存成功')
  } catch (error) {
    operation.value = {
      type: 'error',
      title: errorText(error, '配置保存失败'),
      description: '表单内容已保留，数据库未确认提交，可以修正后重试。',
    }
  } finally {
    saving.value = false
  }
}

async function restoreDefault(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '恢复系统默认后会停用全局覆盖并清除已保存的全局 API Key。是否继续？',
      '恢复系统默认',
      { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  restoring.value = true
  operation.value = null
  try {
    applyConfig(await updateLlmConfig({ active: false, api_key: '' }))
    operation.value = {
      type: 'info',
      title: '已恢复系统默认配置',
      description: '原全局 API Key 已清除，业务继续使用部署环境中的默认配置。',
    }
  } catch (error) {
    operation.value = {
      type: 'error',
      title: errorText(error, '恢复默认失败'),
      description: '当前配置未确认变更，请刷新状态后再决定是否重试。',
    }
  } finally {
    restoring.value = false
  }
}

watch(
  () => form.provider,
  (provider, previousProvider) => {
    const previous = PROVIDER_PRESETS[previousProvider]
    const next = PROVIDER_PRESETS[provider]
    if (!next) return
    if (!form.base_url || (previous && form.base_url === previous.base_url)) {
      form.base_url = next.base_url
    }
    if (!form.model || (previous && form.model === previous.model)) {
      form.model = next.model
    }
  },
)

onMounted(load)
</script>

<template>
  <div class="llm-page" v-loading="loading">
    <header class="page-header">
      <div>
        <h2>通用 AI 接口配置</h2>
        <p>全平台默认模型配置；个人 API 配置仍保持更高优先级。</p>
      </div>
      <el-tag :type="statusType" effect="light" size="large">
        {{ statusLabel }}
      </el-tag>
    </header>

    <el-alert
      v-if="loadError"
      type="error"
      :title="loadError"
      description="没有覆盖当前输入，可以重新加载或继续编辑。"
      show-icon
      :closable="false"
      class="page-alert"
    >
      <template #default>
        <el-button :icon="Refresh" size="small" @click="load">重新加载</el-button>
      </template>
    </el-alert>

    <el-alert
      v-if="fallbackLabel"
      type="warning"
      :title="fallbackLabel"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <el-card shadow="never" class="config-card">
      <el-form label-position="top" class="config-form" @submit.prevent>
        <div class="form-grid">
          <el-form-item label="接口类型">
            <el-select v-model="form.provider" aria-label="接口类型">
              <el-option label="DeepSeek" value="deepseek" />
              <el-option label="OpenAI" value="openai" />
              <el-option label="OpenAI 兼容接口" value="custom" />
            </el-select>
          </el-form-item>

          <el-form-item label="应用状态">
            <el-switch
              v-model="form.active"
              active-text="启用全局覆盖"
              inactive-text="使用系统默认"
            />
          </el-form-item>

          <el-form-item label="Base URL" class="span-two" required>
            <el-input
              v-model="form.base_url"
              maxlength="512"
              placeholder="https://api.example.com/v1"
              clearable
            />
          </el-form-item>

          <el-form-item label="API Key" class="span-two">
            <el-input
              v-model="form.api_key"
              type="password"
              show-password
              maxlength="512"
              autocomplete="new-password"
              :placeholder="cfg?.is_set
                ? `已配置 ${cfg.api_key_masked}，留空保持不变`
                : '填写该端点的 API Key'"
            />
          </el-form-item>

          <el-form-item label="当前模型" class="span-two" required>
            <div class="model-control">
              <el-select
                v-model="form.model"
                filterable
                allow-create
                default-first-option
                placeholder="拉取或手工输入模型名称"
                aria-label="当前模型"
              >
                <el-option
                  v-for="model in modelOptions"
                  :key="model"
                  :label="model"
                  :value="model"
                />
              </el-select>
              <el-tooltip content="从当前接口拉取可用模型" placement="top">
                <el-button
                  :icon="Download"
                  :loading="fetchingModels"
                  :disabled="testing || saving"
                  @click="pullModels"
                >
                  拉取模型
                </el-button>
              </el-tooltip>
            </div>
          </el-form-item>

          <el-form-item label="超时时间（秒）">
            <el-input-number
              v-model="form.timeout_seconds"
              :min="5"
              :max="600"
              :step="5"
              controls-position="right"
            />
          </el-form-item>

          <el-form-item label="最大重试次数">
            <el-input-number
              v-model="form.max_retries"
              :min="0"
              :max="5"
              :step="1"
              controls-position="right"
            />
          </el-form-item>

          <el-form-item label="温度系数">
            <el-input-number
              v-model="form.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              :precision="1"
              controls-position="right"
            />
          </el-form-item>
        </div>

        <el-alert
          v-if="operation"
          :type="operation.type"
          :title="operation.title"
          :description="operation.description"
          show-icon
          closable
          class="operation-alert"
          @close="operation = null"
        />

        <div class="actions">
          <el-button
            type="primary"
            :icon="Check"
            :loading="saving"
            :disabled="testing || fetchingModels || restoring"
            @click="save"
          >
            保存配置
          </el-button>
          <el-button
            :icon="Connection"
            :loading="testing"
            :disabled="saving || fetchingModels || restoring"
            @click="test"
          >
            测试连接
          </el-button>
          <el-button
            v-if="cfg?.is_set || cfg?.source === 'global'"
            :icon="Refresh"
            :loading="restoring"
            :disabled="saving || testing || fetchingModels"
            @click="restoreDefault"
          >
            恢复系统默认
          </el-button>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.llm-page {
  width: min(1120px, 100%);
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 18px;

  h2 {
    margin: 0;
    font-size: 24px;
    line-height: 1.25;
    letter-spacing: 0;
  }

  p {
    margin: 7px 0 0;
    color: var(--el-text-color-secondary);
    font-size: 13px;
  }
}

.page-alert {
  margin-bottom: 14px;
}

.config-card {
  border-radius: 8px;

  :deep(.el-card__body) {
    padding: 24px;
  }
}

.config-form {
  :deep(.el-form-item__label) {
    font-weight: 600;
  }
}

.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 2px 24px;
}

.span-two {
  grid-column: 1 / -1;
}

.model-control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  width: 100%;
}

:deep(.el-select),
:deep(.el-input-number) {
  width: 100%;
}

.operation-alert {
  margin-top: 4px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 22px;
}

@media (max-width: 720px) {
  .page-header {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .page-header :deep(.el-tag) {
    align-self: flex-start;
    max-width: 100%;
    height: auto;
    white-space: normal;
  }

  .config-card :deep(.el-card__body) {
    padding: 16px;
  }

  .form-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }

  .span-two {
    grid-column: auto;
  }

  .model-control {
    grid-template-columns: minmax(0, 1fr);
  }

  .model-control .el-button,
  .actions .el-button {
    width: 100%;
    margin-left: 0;
  }
}
</style>
