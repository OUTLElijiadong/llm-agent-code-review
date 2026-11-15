<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import type { FormInstance, FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { getRoleHomePath } from '@/utils/roleHome'
import { ElMessage } from 'element-plus/es/components/message/index'

const router = useRouter()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const loginError = ref('')
const loginStatus = ref('')

const cooldownStorageKey = 'prism:login-cooldown-until'
const cooldownUntil = ref(0)
const currentTime = ref(Date.now())
const cooldownRemaining = computed(() => Math.max(0, Math.ceil((cooldownUntil.value - currentTime.value) / 1000)))
const statusText = computed(() => cooldownRemaining.value > 0
  ? `登录冷却中，还需等待 ${cooldownRemaining.value} 秒。冷却期间提交正确密码也会被拒绝。`
  : loginStatus.value)
let cooldownTimer: ReturnType<typeof setInterval> | undefined

function stopCooldownTimer(): void {
  if (cooldownTimer !== undefined) clearInterval(cooldownTimer)
  cooldownTimer = undefined
}

function refreshCooldown(): void {
  currentTime.value = Date.now()
  if (cooldownRemaining.value === 0 && cooldownUntil.value > 0) {
    cooldownUntil.value = 0
    stopCooldownTimer()
    try { sessionStorage.removeItem(cooldownStorageKey) } catch { /* 存储不可用不影响服务端限流。 */ }
    loginError.value = ''
    loginStatus.value = '等待时间已结束，可以重新登录。'
  }
}

function startCooldown(until: number): void {
  stopCooldownTimer()
  cooldownUntil.value = until
  refreshCooldown()
  if (cooldownRemaining.value > 0) {
    try { sessionStorage.setItem(cooldownStorageKey, String(until)) } catch { /* 仅保存等待截止时间。 */ }
    cooldownTimer = setInterval(refreshCooldown, 1000)
  }
}

onMounted(() => {
  try {
    const until = Number(sessionStorage.getItem(cooldownStorageKey))
    if (Number.isSafeInteger(until) && until > 0) startCooldown(until)
  } catch { /* 浏览器禁用存储时仍显示本次响应的倒计时。 */ }
})
onBeforeUnmount(stopCooldownTimer)


const form = reactive({
  username: '',
  password: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入账号', trigger: 'blur' },
    { min: 3, max: 32, message: '账号长度在 3 到 32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 64, message: '密码长度最多 64 个字符', trigger: 'blur' },
  ],
}

/**
 * 校验登录表单并进入目标页面
 * @returns Promise<void>
 */
async function handleLogin(): Promise<void> {
  refreshCooldown()
  if (!formRef.value || loading.value || cooldownRemaining.value > 0) return
  loading.value = true
  loginError.value = ''
  loginStatus.value = '正在校验登录信息…'
  try {
    const valid = await formRef.value.validate().catch(() => false)
    if (!valid) {
      loginStatus.value = '请检查账号与密码的填写提示。'
      return
    }
    loginStatus.value = '正在验证账号，请稍候…'
    await userStore.login({ username: form.username, password: form.password })
    loginStatus.value = '登录成功，正在进入工作台…'
    ElMessage.success('登录成功')
    await router.replace(getRoleHomePath(userStore.profile?.role))
  } catch (error: unknown) {
    const failure = error as { code?: unknown; message?: unknown; retry_after_seconds?: unknown } | null
    loginError.value = typeof failure?.message === 'string' && failure.message.trim()
      ? failure.message
      : '登录失败，请检查网络连接后重试。'
    loginStatus.value = '登录未完成，请查看错误提示。'
    const seconds = failure?.retry_after_seconds
    if (typeof seconds === 'number' && Number.isSafeInteger(seconds) && seconds > 0) {
      const until = Date.now() + seconds * 1000
      if (Number.isSafeInteger(until)) {
        if (failure?.code !== 50301) {
          loginError.value = '当前登录入口暂时受限，请等待倒计时结束后再试。'
        }
        startCooldown(until)
      }
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <main class="form-wrap">
      <section class="form-card" aria-label="登录">
        <div class="auth-brand">
          <span class="prism-mark sm on-light"></span>
          <span class="auth-brand-name font-display">Prism · 棱镜</span>
        </div>
        <h1 class="form-title font-display">登录</h1>
        <p class="form-sub">
          没有账号？<RouterLink class="link" to="/register">注册</RouterLink>
        </p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          :disabled="loading"
          :aria-busy="loading"
          label-position="top"
          class="prism-form"
          @submit.prevent="handleLogin"
        >
          <el-form-item prop="username" label="账号">
            <el-input
              v-model="form.username"
              size="large"
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item prop="password" label="密码">
            <el-input
              v-model="form.password"
              type="password"
              size="large"
              show-password
              autocomplete="current-password"
            />
          </el-form-item>

          <div class="login-action-slot">
            <button
              type="submit"
              class="btn-login font-display"
              :disabled="loading || cooldownRemaining > 0"
              :aria-busy="loading"
            >
              <span>{{ cooldownRemaining > 0 ? `等待 ${cooldownRemaining} 秒` : loading ? '正在登录…' : loginError ? '重新登录' : '进入棱镜' }}</span>
              <span v-if="loading" class="login-dots" aria-hidden="true"><span></span><span></span><span></span></span>
            </button>
          </div>
        </el-form>

        <p class="login-status" role="status" aria-live="polite" aria-atomic="true">{{ statusText }}</p>
        <div v-if="loginError" class="login-error" role="alert">{{ loginError }}</div>
      </section>
    </main>
  </div>
</template>

<style scoped lang="scss">
.login-page {
  display: grid;
  place-items: center;
  min-height: 100vh;
  min-height: 100dvh;
  padding: 32px 20px;
  background: var(--app-bg-soft, #f7f8fa);
}

.form-wrap {
  width: min(100%, 420px);
}

.form-card {
  padding: 32px;
  border: 1px solid var(--gray-200, #e5e7eb);
  border-radius: 16px;
  background: #fff;
}

.auth-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 32px;
}

.auth-brand-name {
  color: var(--gray-900, #1f2937);
  font-size: 16px;
  font-weight: 600;
}

.form-title {
  margin: 0;
  color: var(--gray-900, #1f2937);
  font-size: 28px;
  font-weight: 600;
}

.form-sub {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 0 28px;
  color: var(--gray-600, #606266);
  font-size: 14px;
}

.link {
  color: var(--brand-600, #514be2);
  text-decoration: none;
}

.link:hover { text-decoration: underline; }

.link:focus-visible,
.btn-login:focus-visible {
  outline: 3px solid var(--brand-400, #8e88f5);
  outline-offset: 3px;
}

.prism-form {
  :deep(.el-form-item) { margin-bottom: 18px; }

  :deep(.el-form-item__label) {
    padding-bottom: 6px;
    color: var(--gray-700, #4b5563);
    font-size: 13px;
    line-height: 1.4;
  }

  :deep(.el-input__wrapper) {
    height: 44px;
    padding: 4px 12px;
    border-radius: 9px;
    box-shadow: 0 0 0 1px var(--gray-200, #e5e7eb) inset;
  }

  :deep(.el-input__wrapper:hover) {
    box-shadow: 0 0 0 1px var(--gray-300, #d1d5db) inset;
  }

  :deep(.el-input__wrapper.is-focus) {
    box-shadow: 0 0 0 1px var(--brand-400, #8e88f5) inset, 0 0 0 3px rgba(91, 88, 232, 0.12);
  }
}

.login-action-slot { margin-top: 4px; }

.btn-login {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-height: 46px;
  border: 0;
  border-radius: 9px;
  background: var(--gray-900, #1f2937);
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 120ms ease;
}

.btn-login:hover:not(:disabled) { background: var(--brand-600, #514be2); }
.btn-login:disabled { cursor: not-allowed; opacity: 0.65; }

.login-dots {
  display: inline-flex;
  gap: 4px;

  span {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: currentColor;
    animation: loginPulse 1s ease-in-out infinite;
  }

  span:nth-child(2) { animation-delay: 120ms; }
  span:nth-child(3) { animation-delay: 240ms; }
}

@keyframes loginPulse {
  50% { opacity: 0.4; transform: translateY(-2px); }
}

.login-status {
  margin: 12px 0 0;
  color: var(--gray-600, #606266);
  font-size: 13px;
  line-height: 1.5;
}

.login-status:empty { display: none; }

.login-error {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #f0c4cc;
  border-radius: 8px;
  background: #fff4f5;
  color: #a62b43;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

@media (max-width: 480px) {
  .form-card { padding: 24px 20px; }
}

@media (prefers-reduced-motion: reduce) {
  .login-dots span,
  .btn-login { animation: none !important; transition: none !important; }
}
</style>
