<template>
  <div class="api-config-page">
    <div class="page-header">
      <h2>API 配置</h2>
      <p class="page-sub">配置个人大模型 API，不配置则使用平台默认 DeepSeek API</p>
    </div>

    <el-card shadow="hover" class="config-card" v-loading="pageLoading">
      <!-- 当前配置状态 -->
      <div class="current-status">
        <div class="status-left">
          <span class="status-label">当前 API 来源：</span>
          <el-tag :type="currentConfig.is_custom ? 'success' : 'info'" size="default">
            {{ currentConfig.is_custom ? '🔑 个人自定义' : '☁️ 系统默认 (DeepSeek)' }}
          </el-tag>
        </div>
        <div class="status-right">
          <span v-if="currentConfig.is_custom" class="status-model">
            模型：{{ currentConfig.model }}
          </span>
        </div>
      </div>

      <el-divider />

      <!-- 配置表单 -->
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="120px"
        class="config-form"
      >
        <el-form-item label="提供商" prop="provider">
          <el-select v-model="form.provider" style="width: 260px">
            <el-option label="DeepSeek" value="deepseek" />
            <el-option label="OpenAI 兼容" value="openai" />
            <el-option label="自定义" value="custom" />
          </el-select>
          <span class="form-tip">
            {{ providerHint }}
          </span>
        </el-form-item>

        <el-form-item label="API Key" prop="api_key">
          <el-input
            v-model="form.api_key"
            :type="showKey ? 'text' : 'password'"
            placeholder="sk-xxxxxxxx"
            show-password
            maxlength="256"
          >
            <template #append>
              <el-button
                :icon="showKey ? 'View' : 'Hide'"
                @click="showKey = !showKey"
                text
              />
            </template>
          </el-input>
        </el-form-item>

        <el-form-item label="API 地址" prop="base_url">
          <el-input
            v-model="form.base_url"
            placeholder="https://api.deepseek.com"
            maxlength="512"
          />
          <span class="form-tip">无需包含 /chat/completions 路径</span>
        </el-form-item>

        <el-form-item label="模型名称" prop="model">
          <el-input
            v-model="form.model"
            placeholder="deepseek-chat"
            maxlength="128"
          />
          <span class="form-tip">{{ modelHint }}</span>
        </el-form-item>

        <el-form-item>
          <div class="form-actions">
            <el-button
              type="default"
              :loading="testing"
              :disabled="!form.api_key"
              @click="handleTest"
            >
              {{ testing ? '测试中...' : '🔗 测试连接' }}
            </el-button>
            <el-button type="primary" :loading="saving" @click="handleSave">
              {{ saving ? '保存中...' : '💾 保存配置' }}
            </el-button>
            <el-button
              v-if="currentConfig.is_custom"
              type="danger"
              plain
              :loading="resetting"
              @click="handleReset"
            >
              恢复系统默认
            </el-button>
          </div>
        </el-form-item>
      </el-form>

      <!-- 测试结果 -->
      <div v-if="testResult !== null" class="test-result">
        <el-alert
          :title="testResult.message"
          :type="testResult.success ? 'success' : 'error'"
          :closable="true"
          show-icon
          @close="testResult = null"
        >
          <template v-if="testResult.success">
            <p>模型：{{ testResult.model }} | 耗时：{{ testResult.duration_ms }}ms</p>
          </template>
        </el-alert>
      </div>

      <el-divider />

      <!-- 使用说明 -->
      <el-collapse>
        <el-collapse-item title="💡 使用说明">
          <div class="help-text">
            <h4>三种使用方式</h4>
            <ul>
              <li>
                <strong>系统默认</strong>：不配置任何 API，直接使用平台提供的
                DeepSeek API（由管理员统一管理）。
              </li>
              <li>
                <strong>DeepSeek 个人 Key</strong>：在
                <a href="https://platform.deepseek.com" target="_blank" rel="noopener noreferrer">DeepSeek 开放平台</a>
                获取 API Key，填入后即刻生效。审查、聊天、安全扫描均使用您的个人配额。
              </li>
              <li>
                <strong>OpenAI 兼容</strong>：支持公网 OpenAI 兼容接口（如 OpenRouter、
                DeepSeek 兼容端点等），填入对应地址和模型名即可。
              </li>
            </ul>
            <h4>安全说明</h4>
            <ul>
              <li>API Key 使用 AES-128-CBC + HMAC 加密存储，密钥由平台 JWT_SECRET 派生。</li>
              <li>页面中显示的 Key 始终脱敏（仅显示前 5 位和后 4 位）。</li>
              <li>测试连接不会存储任何数据，仅验证连通性和认证。</li>
              <li>为防止 SSRF，默认禁止 API 端点指向 localhost、内网或链路本地地址；如需内网模型服务，请由管理员显式配置后端开关。</li>
              <li>删除配置后立即恢复使用平台默认 API，加密存储的 Key 从数据库彻底删除。</li>
            </ul>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { type FormInstance, type FormRules } from 'element-plus'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'
import {
  getApiConfig,
  saveApiConfig,
  deleteApiConfig,
  testApiConnection,
  type ApiConfigOut,
} from '@/api/apiConfig'

const formRef = ref<FormInstance>()
const pageLoading = ref(true)
const saving = ref(false)
const testing = ref(false)
const resetting = ref(false)
const showKey = ref(false)
const testResult = ref<{ success: boolean; message: string; model: string; duration_ms: number } | null>(null)

const currentConfig = reactive<ApiConfigOut>({
  provider: 'deepseek',
  api_key_masked: '',
  base_url: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  is_active: true,
  is_custom: false,
})

const form = reactive({
  provider: 'deepseek',
  api_key: '',
  base_url: 'https://api.deepseek.com',
  model: 'deepseek-chat',
})

const rules: FormRules = {
  api_key: [
    { required: true, message: '请输入 API Key', trigger: 'blur' },
    { min: 3, message: 'API Key 至少 3 个字符', trigger: 'blur' },
  ],
  base_url: [
    { required: true, message: '请输入 API 地址', trigger: 'blur' },
  ],
  model: [
    { required: true, message: '请输入模型名称', trigger: 'blur' },
  ],
}

const providerHint = computed(() => {
  switch (form.provider) {
    case 'deepseek':
      return '使用 DeepSeek 官方 API'
    case 'openai':
      return '兼容 OpenAI 格式 (Ollama / vLLM / OpenRouter 等)'
    case 'custom':
      return '手动填写任意地址和模型'
    default:
      return ''
  }
})

const modelHint = computed(() => {
  switch (form.provider) {
    case 'deepseek':
      return '推荐 deepseek-chat 或 deepseek-reasoner'
    case 'openai':
      return '如 gpt-4o / qwen2.5 / llama3 等'
    default:
      return '根据您的 API 提供商填写'
  }
})

async function loadConfig(): Promise<void> {
  pageLoading.value = true
  try {
    const cfg = await getApiConfig()
    Object.assign(currentConfig, cfg)
    if (cfg.is_custom) {
      form.provider = cfg.provider
      form.base_url = cfg.base_url
      form.model = cfg.model
      // Key 已脱敏，不在 form 中回填
    }
  } catch {
    /* 网络/权限错误由 http 拦截器提示;表单保持默认状态,用户可直接填写新配置 */
  } finally {
    pageLoading.value = false
  }
}

async function handleTest(): Promise<void> {
  const valid = await formRef.value?.validateField('api_key').catch(() => false)
  if (!valid && form.api_key.length < 3) {
    ElMessage.warning('请先填写 API Key')
    return
  }

  testing.value = true
  testResult.value = null
  try {
    const result = await testApiConnection({
      provider: form.provider,
      api_key: form.api_key,
      base_url: form.base_url,
      model: form.model,
    })
    testResult.value = result
    if (result.success) {
      ElMessage.success('连接测试通过 ✅')
    } else {
      ElMessage.error(result.message)
    }
  } catch {
    testResult.value = {
      success: false,
      message: '测试请求失败，请检查网络',
      model: '',
      duration_ms: 0,
    }
    /* 详细错误已由 http 拦截器弹出 */
  } finally {
    testing.value = false
  }
}

async function handleSave(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  saving.value = true
  try {
    const result = await saveApiConfig({
      provider: form.provider,
      api_key: form.api_key,
      base_url: form.base_url,
      model: form.model,
    })
    Object.assign(currentConfig, result)
    form.api_key = '' // 保存后清空输入框中的 Key
    ElMessage.success('API 配置已保存，即刻生效 🚀')
  } catch (e: any) {
    ElMessage.error(e?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function handleReset(): Promise<void> {
  const ok = await confirmDanger({
    target: '恢复系统默认 API 配置',
    consequence: '您的自定义 Key 将被彻底删除',
    confirmText: '确认重置',
  })
  if (!ok) return

  resetting.value = true
  try {
    await deleteApiConfig()
    Object.assign(currentConfig, {
      provider: 'deepseek',
      api_key_masked: '',
      base_url: 'https://api.deepseek.com',
      model: 'deepseek-chat',
      is_active: true,
      is_custom: false,
    })
    form.provider = 'deepseek'
    form.api_key = ''
    form.base_url = 'https://api.deepseek.com'
    form.model = 'deepseek-chat'
    ElMessage.success('已恢复系统默认 API 配置')
  } catch (e: any) {
    ElMessage.error(e?.message || '重置失败')
  } finally {
    resetting.value = false
  }
}

onMounted(loadConfig)
</script>

<style scoped lang="scss">
.api-config-page {
  padding: var(--spacing-lg);
  max-width: 800px;
}

.page-header {
  margin-bottom: var(--spacing-lg);

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
  }
}

.config-card {
  :deep(.el-card__body) {
    padding: var(--spacing-lg);
  }
}

.current-status {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--color-bg-light, #f5f7fa);
  border-radius: 8px;

  .status-label {
    font-weight: 500;
    margin-right: 8px;
  }

  .status-model {
    font-size: 13px;
    color: var(--color-text-secondary, #909399);
  }
}

.config-form {
  margin-top: var(--spacing-md);

  .form-tip {
    margin-left: 8px;
    font-size: 12px;
    color: var(--color-text-secondary, #909399);
  }
}

.form-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.test-result {
  margin-top: var(--spacing-md);
}

.help-text {
  font-size: 13px;
  line-height: 1.8;
  color: var(--color-text-secondary, #606266);

  h4 {
    margin: 12px 0 6px;
    font-size: 14px;
    color: var(--color-text-primary, #303133);
  }

  ul {
    padding-left: 20px;
    margin: 4px 0;
  }

  li {
    margin: 4px 0;
  }

  a {
    color: var(--el-color-primary);
  }
}
</style>
