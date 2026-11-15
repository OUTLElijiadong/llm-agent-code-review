<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { Refresh } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { getCaptcha } from '@/api/auth'
import { ElMessage } from 'element-plus/es/components/message/index'

const router = useRouter()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  email: '',
  nickname: '',
  beta_code: '',
  captcha_answer: '',
})

// 位图挑战仅作注册防护的一环，不代表强人机验证。
const captcha = ref<{ captcha_id: string; question: string; image_data: string; beta_registration_enabled: boolean }>({
  captcha_id: '',
  question: '',
  image_data: '',
  beta_registration_enabled: false,
})
const captchaLoading = ref(false)
const captchaImageFailed = ref(false)
const captchaImage = computed(() => {
  const image = captcha.value.image_data || ''
  return image.length <= 32768 && /^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(image) ? image : ''
})
const captchaReady = computed(() => !captchaLoading.value && !captchaImageFailed.value && Boolean(captcha.value.captcha_id && captchaImage.value))

async function refreshCaptcha(): Promise<void> {
  captchaLoading.value = true
  captchaImageFailed.value = false
  form.captcha_answer = ''
  try {
    captcha.value = await getCaptcha()
  } catch {
    captcha.value = {
      captcha_id: '',
      question: '加载失败,请重试',
      image_data: '',
      beta_registration_enabled: false,
    }
  } finally {
    captchaLoading.value = false
  }
}

onMounted(refreshCaptcha)

/**
 * 校验确认密码是否与密码一致
 * @param _rule - Element Plus 表单规则对象
 * @param value - 确认密码输入值
 * @param callback - 校验完成回调
 * @returns void
 */
function validateConfirmPassword(_rule: unknown, value: string, callback: (e?: Error) => void): void {
  if (value !== form.password) {
    callback(new Error('两次输入的密码不一致'))
    return
  }
  callback()
}

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '用户名长度在 3 到 32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 15, max: 64, message: '密码长度在 15 到 64 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    { validator: validateConfirmPassword, trigger: 'blur' },
  ],
  email: [
    {
      type: 'email',
      transform: (value: unknown) => String(value ?? '').trim(),
      message: '请输入有效的邮箱地址',
      trigger: 'blur',
    },
  ],
  captcha_answer: [
    { required: true, message: '请输入人机验证结果', trigger: 'blur' },
  ],
  beta_code: [
    { required: true, message: '请输入管理员提供的内测码', trigger: 'blur' },
    {
      pattern: /^PRISM(?:-[A-Z2-9]{5}){4}$/i,
      message: '内测码格式不正确',
      trigger: 'blur',
    },
  ],
}

/**
 * 提交注册表单，注册成功后跳转登录页
 * @returns Promise<void>
 */
async function handleRegister(): Promise<void> {
  if (!formRef.value || !captchaReady.value || loading.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await userStore.register({
        username: form.username,
        password: form.password,
        email: form.email.trim() || undefined,
        nickname: form.nickname || undefined,
        beta_code: captcha.value.beta_registration_enabled
          ? form.beta_code.trim().toUpperCase()
          : undefined,
        captcha_id: captcha.value.captcha_id,
        captcha_answer: form.captcha_answer,
      })
      ElMessage.success('注册成功，请登录')
      router.replace('/login')
    } catch {
      /* 请求拦截器会展示后端返回的错误信息，避免重复 toast。 */
      // 验证码一次性失效,失败后刷新一题
      refreshCaptcha()
    } finally {
      loading.value = false
    }
  })
}

/**
 * 跳转到登录页
 * @returns void
 */
function goLogin(): void {
  router.push('/login')
}
</script>

<template>
  <div class="register-page">
    <main class="form-wrap">
      <section class="form-panel" aria-label="注册账号">
        <div class="auth-brand">
          <span class="prism-mark sm on-light"></span>
          <span class="auth-brand-name font-display">Prism · 棱镜</span>
        </div>
        <h1 class="form-title font-display">创建账号</h1>
        <p class="form-sub">
          已有账号？<button type="button" class="text-link" @click="goLogin">登录</button>
        </p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          :aria-busy="loading"
          class="prism-form"
          label-position="top"
          @keyup.enter="handleRegister"
        >
          <el-form-item prop="username" label="账号">
            <el-input v-model="form.username" size="large" autocomplete="username" />
          </el-form-item>

          <el-form-item prop="nickname" label="昵称（选填）">
            <el-input v-model="form.nickname" size="large" autocomplete="name" />
          </el-form-item>

          <el-form-item prop="email" label="邮箱（选填）">
            <el-input
              v-model="form.email"
              size="large"
              autocomplete="email"
              inputmode="email"
              @blur="form.email = form.email.trim()"
            />
          </el-form-item>

          <el-form-item prop="password" label="密码">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="15–64 个字符"
              size="large"
              show-password
              maxlength="64"
              autocomplete="new-password"
            />
          </el-form-item>

          <el-form-item prop="confirmPassword" label="确认密码">
            <el-input
              v-model="form.confirmPassword"
              type="password"
              placeholder="再次输入密码"
              size="large"
              show-password
              autocomplete="new-password"
            />
          </el-form-item>

          <el-form-item v-if="captcha.beta_registration_enabled" prop="beta_code" label="内测码">
            <el-input
              v-model="form.beta_code"
              placeholder="管理员提供的内测码"
              autocomplete="off"
              maxlength="29"
              class="beta-code-input"
              @blur="form.beta_code = form.beta_code.trim().toUpperCase()"
            />
          </el-form-item>

          <el-form-item prop="captcha_answer" label="验证码">
            <div class="captcha-row">
              <div class="captcha-q" :class="{ loading: captchaLoading }">
                <img v-if="captchaImage && !captchaImageFailed" :src="captchaImage" alt="注册验证码图片" width="168" height="56" @error="captchaImageFailed = true" />
                <span v-else>{{ captchaLoading ? '正在加载…' : '加载失败，请刷新' }}</span>
              </div>
              <el-input
                v-model="form.captcha_answer"
                placeholder="验证码"
                aria-label="验证码，不区分大小写"
                maxlength="6"
                :disabled="!captchaReady"
                size="large"
                autocomplete="off"
                class="captcha-answer"
              />
              <el-button
                :icon="Refresh"
                circle
                aria-label="刷新验证码"
                :loading="captchaLoading"
                @click="refreshCaptcha"
              />
            </div>
          </el-form-item>

          <button
            type="button"
            class="btn-register font-display"
            :disabled="loading || !captchaReady"
            @click="handleRegister"
          >
            {{ loading ? '正在创建…' : '创建账号' }}
          </button>
        </el-form>
      </section>
    </main>
  </div>
</template>

<style scoped lang="scss">
.register-page {
  min-height: 100vh;
  min-height: 100dvh;
  background: var(--app-bg-soft, #f7f8fa);
}

.form-wrap {
  display: grid;
  place-items: center;
  min-height: 100vh;
  min-height: 100dvh;
  padding: 32px 20px;
}

.form-panel {
  width: min(100%, 460px);
  padding: 32px;
  border: 1px solid var(--gray-200, #e5e7eb);
  border-radius: 16px;
  background: #fff;
}

.auth-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 28px;
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
  margin: 8px 0 24px;
  color: var(--gray-600, #606266);
  font-size: 14px;
}

.text-link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--brand-600, #514be2);
  font: inherit;
  cursor: pointer;
}

.text-link:hover { text-decoration: underline; }

.text-link:focus-visible,
.btn-register:focus-visible {
  outline: 3px solid var(--brand-400, #8e88f5);
  outline-offset: 3px;
}

.beta-code-input :deep(input) {
  font-family: var(--font-mono);
  text-transform: uppercase;
}

.prism-form {
  :deep(.el-form-item) { margin-bottom: 16px; }

  :deep(.el-form-item__label) {
    padding-bottom: 5px;
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

.captcha-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.captcha-q {
  display: grid;
  flex: 0 0 144px;
  place-items: center;
  width: 144px;
  height: 48px;
  overflow: hidden;
  border-radius: 8px;
  background: var(--gray-100, #f5f7fa);
  color: var(--gray-700, #4b5563);
  font-size: 13px;
  user-select: none;
}

.captcha-q img { display: block; width: 144px; height: 48px; }
.captcha-q.loading { opacity: 0.55; }
.captcha-answer { flex: 1; min-width: 80px; }

.btn-register {
  width: 100%;
  min-height: 46px;
  margin-top: 4px;
  border: 0;
  border-radius: 9px;
  background: var(--gray-900, #1f2937);
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 120ms ease;
}

.btn-register:hover:not(:disabled) { background: var(--brand-600, #514be2); }
.btn-register:disabled { cursor: not-allowed; opacity: 0.65; }

@media (max-width: 480px) {
  .form-panel { padding: 24px 20px; }
  .captcha-row { gap: 8px; }
  .captcha-q { flex-basis: 128px; width: 128px; height: 44px; }
  .captcha-q img { width: 128px; height: 44px; }
}

@media (max-width: 360px) {
  .form-panel { padding: 20px 16px; }
  .captcha-row { gap: 6px; }
  .captcha-q { flex-basis: 104px; width: 104px; height: 40px; }
  .captcha-q img { width: 104px; height: 40px; }
  .captcha-answer { min-width: 64px; }
}

@media (prefers-reduced-motion: reduce) {
  .btn-register { transition: none !important; }
}
</style>
