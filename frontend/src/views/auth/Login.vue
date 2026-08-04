<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { User, Lock } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { getRoleHomePath } from '@/utils/roleHome'
import { ElMessage } from 'element-plus/es/components/message/index'

const router = useRouter()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '用户名长度在 3 到 32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度在 6 到 32 个字符', trigger: 'blur' },
  ],
}

/**
 * 校验登录表单并进入目标页面
 * @returns Promise<void>
 */
async function handleLogin(): Promise<void> {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await userStore.login({ username: form.username, password: form.password })
      ElMessage.success('登录成功')
      // 登录后一律回角色首页(工作台/总览),不跟随 redirect——
      // 避免从旧链接/过期会话跳转时落到非预期页面。
      router.replace(getRoleHomePath(userStore.profile?.role))
    } catch {
      /* 请求拦截器会展示后端返回的错误信息，避免重复 toast。 */
    } finally {
      loading.value = false
    }
  })
}

/**
 * 跳转到注册页
 * @returns void
 */
function goRegister(): void {
  router.push('/register')
}
</script>

<template>
  <div class="login-page">
    <!-- =========== 左侧品牌区 =========== -->
    <aside class="brand">
      <header class="brand-top">
        <span class="prism-mark"></span>
        <span class="brand-name font-display">Prism · 棱镜</span>
      </header>

      <div class="brand-center">
        <div class="brand-eyebrow font-mono">AI CODE REVIEW · POWERED BY DEEPSEEK</div>
        <h1 class="brand-title font-display">
          让代码穿过<br>
          <em>棱镜</em>，<br>
          折射真相。
        </h1>
        <p class="brand-sub">
          大模型智能体替你读完每一行代码，并用自然语言告诉你：哪里有问题、为什么有问题、怎么改才更好。
          比 SonarQube 更懂语义，比人工 Review 快 10 倍。
        </p>

        <div class="brand-spectrum">
          <div v-for="(seg, i) in 8" :key="i" :class="`seg seg-${i}`" :style="{ animationDelay: `${i * 0.1}s` }"></div>
        </div>
        <div class="brand-spectrum-labels font-mono">
          <span>规范</span><span>命名</span><span>注释</span><span>维护</span>
          <span>性能</span><span>异常</span><span>Bug</span><span>安全</span>
        </div>
      </div>

      <footer class="brand-bottom font-mono">
        <span class="online-dot">DeepSeek V4 在线</span>
        <span>v1.0 · 2026</span>
      </footer>
    </aside>

    <!-- =========== 右侧表单区 =========== -->
    <main class="form-wrap">
      <div class="form-card">
        <div class="mobile-brand">
          <span class="prism-mark sm on-light"></span>
          <span class="mobile-brand-text font-display">Prism · 棱镜</span>
        </div>
        <div class="form-eyebrow font-mono">WELCOME BACK</div>
        <h2 class="form-title font-display">登录到你的工作台</h2>
        <p class="form-sub">
          还没有账号？
          <a class="link" @click="goRegister">立即注册 →</a>
        </p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          class="prism-form"
          @keyup.enter="handleLogin"
        >
          <el-form-item prop="username" label="账号 / 学号">
            <el-input
              v-model="form.username"
              placeholder="请输入用户名"
              :prefix-icon="User"
              size="large"
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item prop="password" label="密码">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              :prefix-icon="Lock"
              size="large"
              show-password
              autocomplete="current-password"
            />
          </el-form-item>

          <button
            type="button"
            class="btn-login font-display"
            :class="{ loading }"
            :disabled="loading"
            @click="handleLogin"
          >
            <span v-if="!loading">进入棱镜</span>
            <span v-else class="think-dots"><span></span><span></span><span></span></span>
            <span v-if="!loading" class="arrow font-mono">→</span>
          </button>
        </el-form>

        <div class="footer-mini font-mono">© 2026 Prism · 棱镜智能代码审查</div>
      </div>
    </main>
  </div>
</template>

<style scoped lang="scss">
.login-page {
  display: grid;
  grid-template-columns: 1fr 560px;
  min-height: 100vh;
  width: 100%;
  background: #fff;
  overflow: hidden;
}

@media (max-width: 1280px) {
  .login-page { grid-template-columns: 1fr 480px; }
}
@media (max-width: 900px) {
  .login-page { grid-template-columns: 1fr; }
  .brand { display: none !important; }
}

/* ============ 左侧品牌区 ============ */
.brand {
  position: relative;
  overflow: hidden;
  background: linear-gradient(165deg, #1A1E2C 0%, #161A24 55%, #1F1A3A 100%);
  color: #fff;
  padding: 48px 64px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

/* 棱镜光晕 */
.brand::before {
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

/* 光束 */
.brand::after {
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

.brand > * { position: relative; z-index: 2; }

.brand-top {
  display: flex;
  align-items: center;
  gap: 14px;
}

.brand-name {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #fff;
}

.brand-center {
  max-width: 520px;
}

.brand-eyebrow {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--brand-300);
  text-transform: uppercase;
}

.brand-title {
  font-size: 52px;
  font-weight: 600;
  line-height: 1.08;
  letter-spacing: 0;
  margin: 16px 0 24px;
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
  font-size: 15px;
  line-height: 1.75;
  color: rgba(255, 255, 255, 0.7);
  max-width: 460px;
}

.brand-spectrum {
  margin-top: 40px;
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 6px;

  .seg {
    height: 4px;
    border-radius: 2px;
    animation: pulseSeg 3s ease-in-out infinite;
  }
  .seg-0 { background: #6B7CFF; }
  .seg-1 { background: #4B9BFF; }
  .seg-2 { background: #2BBFB9; }
  .seg-3 { background: #4FB87A; }
  .seg-4 { background: #D4A53A; }
  .seg-5 { background: #E08648; }
  .seg-6 { background: #E25C73; }
  .seg-7 { background: #B85AC4; }
}

@keyframes pulseSeg {
  0%, 100% { opacity: 0.55; transform: scaleY(1); }
  50%      { opacity: 1;    transform: scaleY(1.6); }
}

.brand-spectrum-labels {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 6px;
  font-size: 10px;
  color: rgba(255, 255, 255, 0.4);
}

.brand-bottom {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.4);
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

/* ============ 右侧表单区 ============ */
.form-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  background: #fff;
}

.form-card {
  width: 100%;
  max-width: 380px;
}

.mobile-brand {
  display: none;
  align-items: center;
  gap: 10px;
  margin-bottom: 26px;
}

.mobile-brand-text {
  font-size: 16px;
  font-weight: 650;
  color: var(--gray-900);
}

.form-eyebrow {
  font-size: 11px;
  letter-spacing: 0.12em;
  color: var(--brand-500);
  text-transform: uppercase;
}

.form-title {
  font-size: 32px;
  font-weight: 600;
  margin: 12px 0 8px;
  color: var(--gray-900);
  letter-spacing: 0;
}

.form-sub {
  color: var(--gray-500);
  font-size: 14px;
  margin-bottom: 28px;
}

.link {
  color: var(--brand-500);
  cursor: pointer;

  &:hover { text-decoration: underline; }
}

.prism-form {
  :deep(.el-form-item__label) {
    font-size: 12px;
    color: var(--gray-600);
    font-weight: 500;
    line-height: 1.4;
    margin-bottom: 4px;
    padding-bottom: 0;
  }

  :deep(.el-input__wrapper) {
    border-radius: 10px;
    box-shadow: 0 0 0 1px var(--gray-200) inset;
    padding: 4px 12px;
    height: 44px;
    transition: all 0.15s ease;
  }

  :deep(.el-input__wrapper:hover) {
    box-shadow: 0 0 0 1px var(--gray-300) inset;
  }

  :deep(.el-input__wrapper.is-focus) {
    box-shadow: 0 0 0 1px var(--brand-400) inset, 0 0 0 4px rgba(91, 88, 232, 0.12);
  }
}

.btn-login {
  width: 100%;
  height: 48px;
  background: var(--gray-900);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  position: relative;
  overflow: hidden;
  transition: all 0.2s ease;
  margin-top: 4px;

  &::after {
    content: '';
    position: absolute;
    top: 0; bottom: 0;
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

  &:hover:not(:disabled)::after { left: 120%; }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.7;
  }
}

.footer-mini {
  text-align: center;
  font-size: 12px;
  color: var(--gray-400);
  margin-top: 32px;
}

@media (max-width: 900px) {
  .form-wrap {
    align-items: flex-start;
    min-height: 100vh;
    padding: 96px 24px 32px;
    background:
      linear-gradient(180deg, #fff 0%, var(--app-bg-soft) 56%, var(--app-bg) 100%);
  }

  .form-card {
    max-width: 420px;
    margin: 0 auto;
  }

  .mobile-brand {
    display: inline-flex;
  }

  .form-title {
    font-size: 30px;
  }

  .prism-form {
    :deep(.el-form-item) {
      display: block;
      margin-bottom: 18px;
    }

    :deep(.el-form-item__label) {
      display: flex;
      justify-content: flex-start;
      width: auto !important;
      height: auto;
      margin-bottom: 8px;
      padding: 0;
    }
  }
}

@media (max-width: 430px) {
  .form-wrap {
    padding: 72px 24px 28px;
  }

  .form-title {
    font-size: 28px;
  }

  .footer-mini {
    margin-top: 26px;
  }
}
</style>
