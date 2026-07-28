<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { Lock } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { changePassword } from '@/api/auth'
import { ElMessage } from 'element-plus/es/components/message/index'
import { goBack as safeGoBack } from '@/utils/navigation'

const router = useRouter()
const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})

/**
 * 自定义校验规则：确认密码必须与新密码一致
 */
const validateConfirmPassword = (_rule: unknown, value: string, callback: (e?: Error) => void) => {
  if (value !== form.newPassword) {
    callback(new Error('两次输入的新密码不一致'))
  } else {
    callback()
  }
}

const rules: FormRules = {
  oldPassword: [
    { required: true, message: '请输入旧密码', trigger: 'blur' },
  ],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度在 6 到 32 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认新密码', trigger: 'blur' },
    { validator: validateConfirmPassword, trigger: 'blur' },
  ],
}

/**
 * 提交修改密码请求
 */
async function handleSubmit() {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await changePassword({
        old_password: form.oldPassword,
        new_password: form.newPassword,
      })
      ElMessage.success('密码修改成功，请重新登录')
      router.push('/login')
    } catch {
      ElMessage.error('密码修改失败，请检查旧密码是否正确')
    } finally {
      loading.value = false
    }
  })
}

function goBack() {
  // 安全返回:直接 URL 进入(无站内历史)时回到个人中心而非离开应用
  safeGoBack(router, '/profile')
}
</script>

<template>
  <div class="change-password-page">
    <div class="page-header">
      <el-button text @click="goBack">
        <el-icon><ArrowLeft /></el-icon>
        返回
      </el-button>
      <h2>修改密码</h2>
    </div>
    <div class="form-container">
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        class="password-form"
        label-width="100px"
      >
        <el-form-item label="旧密码" prop="oldPassword">
          <el-input
            v-model="form.oldPassword"
            type="password"
            placeholder="请输入旧密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item label="新密码" prop="newPassword">
          <el-input
            v-model="form.newPassword"
            type="password"
            placeholder="请输入新密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            placeholder="请确认新密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            @click="handleSubmit"
          >
            确认修改
          </el-button>
          <el-button @click="goBack">取消</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped lang="scss">
.change-password-page {
  padding: var(--spacing-lg);
}

.page-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);

  h2 {
    font-size: var(--font-size-xl);
    font-weight: 600;
    color: var(--color-text-primary);
  }
}

.form-container {
  max-width: 520px;
  padding: 28px var(--spacing-xl);
  background: var(--color-bg-card);
  border-radius: var(--border-radius-lg);
  box-shadow: var(--box-shadow-light);
}

.password-form {
  .el-form-item {
    margin-bottom: 22px;
  }
}
</style>
