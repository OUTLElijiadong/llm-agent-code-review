<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Check, Lock, Message, User, UserFilled } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'

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
})

/**
 * 密码强度评估: 依据长度与字符种类给出 0(空)/1(弱)/2(中)/3(强)
 * @returns 强度等级与中文标签
 */
const passwordStrength = computed<{ level: 0 | 1 | 2 | 3; label: string }>(() => {
  const v = form.password
  if (!v) return { level: 0, label: '' }
  let score = 0
  if (v.length >= 6) score++
  if (v.length >= 10) score++
  if (/\d/.test(v)) score++
  if (/[a-z]/.test(v) && /[A-Z]/.test(v)) score++
  if (/[^A-Za-z0-9]/.test(v)) score++
  const level = (score <= 2 ? 1 : score <= 3 ? 2 : 3) as 1 | 2 | 3
  return { level, label: ['', '弱', '中', '强'][level] }
})

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
    { min: 6, max: 32, message: '密码长度在 6 到 32 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    { validator: validateConfirmPassword, trigger: 'blur' },
  ],
  email: [
    { type: 'email', message: '请输入有效的邮箱地址', trigger: 'blur' },
  ],
}

/**
 * 提交注册表单，注册成功后跳转登录页
 * @returns Promise<void>
 */
async function handleRegister(): Promise<void> {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await userStore.register({
        username: form.username,
        password: form.password,
        email: form.email || undefined,
        nickname: form.nickname || undefined,
      })
      ElMessage.success('注册成功，请登录')
      router.replace('/login')
    } catch {
      /* 请求拦截器会展示后端返回的错误信息，避免重复 toast。 */
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
    <aside class="register-brand">
      <header class="brand-top">
        <span class="prism-mark"></span>
        <span class="brand-name font-display">Prism · 棱镜</span>
      </header>

      <div class="brand-center">
        <div class="brand-eyebrow font-mono">AI CODE REVIEW · TEAM ONBOARDING</div>
        <h1 class="brand-title font-display">
          创建账号，<br>
          把代码审查接入<br>
          <em>棱镜工作流。</em>
        </h1>
        <p class="brand-sub">
          注册后即可进入项目空间，上传代码文件，发起多 Agent 审查，并沉淀团队可追踪的质量报告。
        </p>

        <div class="value-list">
          <div class="value-item">
            <span class="value-icon"><el-icon><Check /></el-icon></span>
            <span>项目、代码、审查任务统一管理</span>
          </div>
          <div class="value-item">
            <span class="value-icon"><el-icon><Check /></el-icon></span>
            <span>质量、安全、性能问题自动归档</span>
          </div>
          <div class="value-item">
            <span class="value-icon"><el-icon><Check /></el-icon></span>
            <span>报告与 AI 修复提示词随审查沉淀</span>
          </div>
        </div>
      </div>

      <footer class="brand-bottom font-mono">
        <span class="online-dot">DeepSeek V4 在线</span>
        <span>v1.0 · 2026</span>
      </footer>
    </aside>

    <main class="form-wrap">
      <section class="form-panel" aria-label="注册账号">
        <div class="mobile-brand">
          <span class="prism-mark sm on-light"></span>
          <span class="mobile-brand-text font-display">Prism · 棱镜</span>
        </div>

        <div class="form-eyebrow font-mono">NEW ACCOUNT</div>
        <h2 class="form-title font-display">创建棱镜账号</h2>
        <p class="form-sub">
          已经有账号？
          <button type="button" class="text-link" @click="goLogin">立即登录 →</button>
        </p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          class="prism-form"
          label-position="top"
          @keyup.enter="handleRegister"
        >
          <div class="form-grid">
            <el-form-item prop="username" label="账号 / 学号">
              <el-input
                v-model="form.username"
                placeholder="请输入用户名"
                :prefix-icon="User"
                size="large"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item prop="nickname" label="昵称">
              <el-input
                v-model="form.nickname"
                placeholder="选填"
                :prefix-icon="UserFilled"
                size="large"
                autocomplete="name"
              />
            </el-form-item>
          </div>

          <el-form-item prop="email" label="邮箱">
            <el-input
              v-model="form.email"
              placeholder="选填，用于接收审查通知"
              :prefix-icon="Message"
              size="large"
              autocomplete="email"
            />
          </el-form-item>

          <el-form-item prop="password" label="密码">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入 6-32 位密码"
              :prefix-icon="Lock"
              size="large"
              show-password
              autocomplete="new-password"
            />
          </el-form-item>

          <div v-if="form.password" class="pwd-strength" :data-level="passwordStrength.level">
            <span class="pwd-bar"></span>
            <span class="pwd-bar"></span>
            <span class="pwd-bar"></span>
            <span class="pwd-strength-label font-mono">密码强度 · {{ passwordStrength.label }}</span>
          </div>

          <el-form-item prop="confirmPassword" label="确认密码">
            <el-input
              v-model="form.confirmPassword"
              type="password"
              placeholder="请再次输入密码"
              :prefix-icon="Lock"
              size="large"
              show-password
              autocomplete="new-password"
            />
          </el-form-item>

          <button
            type="button"
            class="btn-register font-display"
            :class="{ loading }"
            :disabled="loading"
            @click="handleRegister"
          >
            <span v-if="!loading">创建账号</span>
            <span v-else class="think-dots"><span></span><span></span><span></span></span>
            <span v-if="!loading" class="arrow font-mono">→</span>
          </button>
        </el-form>

        <div class="footer-mini font-mono">© 2026 Prism · 棱镜智能代码审查</div>
      </section>
    </main>
  </div>
</template>

<style scoped lang="scss">
.register-page {
  display: grid;
  grid-template-columns: 1fr 560px;
  min-height: 100vh;
  width: 100%;
  background: #fff;
  overflow: hidden;
}

.register-brand {
  position: relative;
  overflow: hidden;
  background: linear-gradient(165deg, #1A1E2C 0%, #161A24 55%, #1F1A3A 100%);
  color: #fff;
  padding: 48px 64px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;

  /* 棱镜光晕（与登录页一致） */
  &::before {
    content: '';
    position: absolute;
    width: 720px;
    height: 720px;
    right: -180px;
    top: -200px;
    background: conic-gradient(from 220deg,
      #6B7CFF, #4B9BFF, #2BBFB9, #4FB87A,
      #D4A53A, #E08648, #E25C73, #B85AC4, #6B7CFF);
    filter: blur(80px);
    opacity: 0.45;
    border-radius: 50%;
    pointer-events: none;
  }

  /* 光束（与登录页一致） */
  &::after {
    content: '';
    position: absolute;
    width: 2px;
    height: 60vh;
    left: 30%;
    top: 20%;
    background: linear-gradient(180deg, transparent, rgba(255, 255, 255, 0.6), transparent);
    transform: rotate(15deg);
    filter: blur(0.5px);
    pointer-events: none;
  }
}

.register-brand > * {
  position: relative;
  z-index: 2;
}

.brand-top,
.mobile-brand {
  display: flex;
  align-items: center;
  gap: 14px;
}

.brand-name {
  font-size: 18px;
  font-weight: 600;
  color: #fff;
}

.brand-center {
  max-width: 560px;
}

.brand-eyebrow,
.form-eyebrow {
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.brand-eyebrow {
  color: var(--brand-300);
}

.brand-title {
  font-size: 50px;
  line-height: 1.08;
  font-weight: 600;
  letter-spacing: 0;
  margin: 16px 0 22px;
  color: #fff;

  em {
    font-style: normal;
    background: linear-gradient(120deg, #8E88F5, #54CCDE 50%, #E25C73 90%);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
}

.brand-sub {
  max-width: 500px;
  margin: 0;
  font-size: 15px;
  line-height: 1.78;
  color: rgba(255, 255, 255, 0.72);
}

.value-list {
  display: grid;
  gap: 12px;
  margin-top: 34px;
}

.value-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: fit-content;
  max-width: 100%;
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.78);
  font-size: 13px;
  line-height: 1.5;
}

.value-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: rgba(79, 184, 122, 0.16);
  color: var(--status-fixed);
  flex-shrink: 0;
}

.brand-bottom {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.42);
}

.online-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;

  &::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--status-fixed);
    box-shadow: 0 0 8px var(--status-fixed);
  }
}

.form-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  background:
    linear-gradient(180deg, #fff 0%, var(--app-bg-soft) 68%, var(--app-bg) 100%);
}

.form-panel {
  width: 100%;
  max-width: 460px;
}

.mobile-brand {
  display: none;
  margin-bottom: 26px;
}

.mobile-brand-text {
  font-size: 16px;
  font-weight: 650;
  color: var(--gray-900);
}

.form-eyebrow {
  color: var(--brand-500);
}

.form-title {
  margin: 12px 0 8px;
  color: var(--gray-900);
  font-size: 32px;
  font-weight: 600;
  letter-spacing: 0;
}

.form-sub {
  margin: 0 0 28px;
  color: var(--gray-500);
  font-size: 14px;
}

.text-link {
  padding: 0;
  border: none;
  background: transparent;
  color: var(--brand-500);
  font: inherit;
  cursor: pointer;

  &:hover {
    text-decoration: underline;
  }
}

.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

/* 密码强度指示条 */
.pwd-strength {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: -8px 0 16px;
}

.pwd-bar {
  flex: 1;
  height: 3px;
  border-radius: 2px;
  background: var(--gray-200);
  transition: background 0.2s ease;
}

.pwd-strength-label {
  flex: none;
  margin-left: 4px;
  font-size: 11px;
  color: var(--gray-400);
  letter-spacing: 0.02em;
}

.pwd-strength[data-level='1'] {
  .pwd-bar:nth-child(1) { background: #E25C73; }
  .pwd-strength-label { color: #E25C73; }
}

.pwd-strength[data-level='2'] {
  .pwd-bar:nth-child(-n + 2) { background: #D4A53A; }
  .pwd-strength-label { color: #D4A53A; }
}

.pwd-strength[data-level='3'] {
  .pwd-bar:nth-child(-n + 3) { background: var(--status-fixed); }
  .pwd-strength-label { color: var(--status-fixed); }
}

.prism-form {
  :deep(.el-form-item) {
    margin-bottom: 18px;
  }

  :deep(.el-form-item__label) {
    padding-bottom: 6px;
    color: var(--gray-600);
    font-size: 12px;
    font-weight: 500;
    line-height: 1.4;
  }

  :deep(.el-input__wrapper) {
    height: 44px;
    padding: 4px 12px;
    border-radius: 10px;
    box-shadow: 0 0 0 1px var(--gray-200) inset;
    transition: all 0.15s ease;
  }

  :deep(.el-input__wrapper:hover) {
    box-shadow: 0 0 0 1px var(--gray-300) inset;
  }

  :deep(.el-input__wrapper.is-focus) {
    box-shadow: 0 0 0 1px var(--brand-400) inset, 0 0 0 4px rgba(91, 88, 232, 0.12);
  }
}

.btn-register {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  height: 48px;
  margin-top: 4px;
  overflow: hidden;
  border: none;
  border-radius: 10px;
  background: var(--gray-900);
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;

  &::after {
    content: '';
    position: absolute;
    top: 0;
    bottom: 0;
    left: -100%;
    width: 60%;
    background: linear-gradient(90deg, transparent, rgba(143, 136, 245, 0.4), transparent);
    transform: skewX(-20deg);
    transition: left 0.6s ease;
  }

  &:hover:not(:disabled) {
    background: var(--brand-600);
    transform: translateY(-1px);
    box-shadow: 0 8px 24px -8px rgba(91, 88, 232, 0.5);
  }

  &:hover:not(:disabled)::after {
    left: 120%;
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.7;
  }
}

.think-dots {
  display: inline-flex;
  align-items: center;
  gap: 4px;

  span {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
    animation: dotPulse 0.9s ease-in-out infinite;
  }

  span:nth-child(2) {
    animation-delay: 0.12s;
  }

  span:nth-child(3) {
    animation-delay: 0.24s;
  }
}

@keyframes dotPulse {
  0%, 100% {
    opacity: 0.38;
    transform: translateY(0);
  }

  50% {
    opacity: 1;
    transform: translateY(-2px);
  }
}

.footer-mini {
  margin-top: 30px;
  text-align: center;
  color: var(--gray-400);
  font-size: 12px;
}

@media (max-width: 1280px) {
  .register-page {
    grid-template-columns: 1fr 480px;
  }

  .register-brand {
    padding: 42px 48px;
  }

  .brand-title {
    font-size: 44px;
  }
}

@media (max-width: 900px) {
  .register-page {
    display: block;
    min-height: 100vh;
  }

  .register-brand {
    display: none;
  }

  .form-wrap {
    align-items: flex-start;
    min-height: 100vh;
    padding: 76px 24px 32px;
  }

  .form-panel {
    max-width: 460px;
    margin: 0 auto;
  }

  .mobile-brand {
    display: inline-flex;
  }
}

@media (max-width: 520px) {
  .form-wrap {
    padding: 62px 22px 28px;
  }

  .form-title {
    font-size: 28px;
  }

  .form-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
