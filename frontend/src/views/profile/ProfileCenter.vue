<template>
  <div class="profile-center-page">
    <div class="page-header">
      <h2>个人中心</h2>
      <p class="page-sub">查看账户资料、管理密码与默认审查偏好</p>
    </div>

    <div class="profile-grid">
      <el-card shadow="hover" class="info-card">
        <h3 class="block-title">账户信息</h3>
        <el-descriptions :column="1" border size="default">
          <el-descriptions-item label="用户名">{{ profile?.username || '-' }}</el-descriptions-item>
          <el-descriptions-item label="昵称">{{ profile?.nickname || '-' }}</el-descriptions-item>
          <el-descriptions-item label="邮箱">{{ profile?.email || '-' }}</el-descriptions-item>
          <el-descriptions-item label="角色">
            <el-tag :type="roleTagType" size="small">{{ roleLabel }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="profile?.status === 1 ? 'success' : 'danger'" size="small">
              {{ profile?.status === 1 ? '正常' : '已禁用' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="最近登录">
            {{ formatDate(profile?.last_login) }}
          </el-descriptions-item>
          <el-descriptions-item label="注册时间">
            {{ formatDate(profile?.create_time) }}
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card shadow="hover" class="action-card">
        <h3 class="block-title">账户操作</h3>
        <div class="action-row">
          <div>
            <p class="action-title">修改密码</p>
            <p class="action-desc">建议每 90 天更换一次密码</p>
          </div>
          <el-button type="primary" @click="goChangePassword">前往修改</el-button>
        </div>
        <div class="action-row">
          <div>
            <p class="action-title">退出登录</p>
            <p class="action-desc">清除本机 Token 并返回登录页</p>
          </div>
          <el-button @click="handleLogout">退出</el-button>
        </div>

        <h3 class="block-title" style="margin-top: 24px">默认审查偏好</h3>
        <el-form label-width="120px" class="pref-form">
          <el-form-item label="默认审查类型">
            <el-select v-model="prefs.reviewType" style="width: 220px">
              <el-option label="quick · 快速" value="quick" />
              <el-option label="standard · 标准" value="standard" />
              <el-option label="security · 安全" value="security" />
              <el-option label="performance · 性能" value="performance" />
              <el-option label="full · 全面" value="full" />
            </el-select>
          </el-form-item>
          <el-form-item label="完成通知">
            <el-switch v-model="prefs.notify" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="savePrefs">保存偏好</el-button>
            <span class="pref-tip">偏好保存在本机 localStorage，不上传服务器</span>
          </el-form-item>
        </el-form>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import dayjs from 'dayjs'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const profile = computed(() => userStore.profile)

const roleLabel = computed(() => {
  switch (profile.value?.role) {
    case 'admin':
      return '管理员'
    case 'reviewer':
      return '审查员'
    default:
      return '普通用户'
  }
})

const roleTagType = computed<'danger' | 'warning' | 'primary'>(() => {
  if (profile.value?.role === 'admin') return 'danger'
  if (profile.value?.role === 'reviewer') return 'warning'
  return 'primary'
})

const PREF_KEY = 'prism:user-prefs'

const prefs = reactive({
  reviewType: 'standard',
  notify: true,
})

function formatDate(time?: string): string {
  return time ? dayjs(time).format('YYYY-MM-DD HH:mm') : '-'
}

function loadPrefs(): void {
  try {
    const raw = localStorage.getItem(PREF_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw)
    if (parsed.reviewType) prefs.reviewType = parsed.reviewType
    if (typeof parsed.notify === 'boolean') prefs.notify = parsed.notify
  } catch {
    /* ignore corrupt storage */
  }
}

function savePrefs(): void {
  localStorage.setItem(PREF_KEY, JSON.stringify({ reviewType: prefs.reviewType, notify: prefs.notify }))
  ElMessage.success('偏好已保存')
}

function goChangePassword(): void {
  router.push('/profile/password')
}

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确认退出登录?', '提示', { type: 'warning' })
    userStore.logout()
    router.push('/login')
  } catch {
    /* user cancelled */
  }
}

onMounted(loadPrefs)
</script>

<style scoped lang="scss">
.profile-center-page {
  padding: var(--spacing-lg);
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

.profile-grid {
  display: grid;
  grid-template-columns: 1fr 1.2fr;
  gap: var(--spacing-md);

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }
}

.block-title {
  margin: 0 0 14px;
  font-size: 15px;
  font-weight: 600;
}

.action-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border-light, #ebeef5);

  &:last-of-type {
    border-bottom: none;
  }

  .action-title {
    margin: 0 0 4px;
    font-size: 14px;
    font-weight: 500;
    color: var(--color-text-primary, #303133);
  }

  .action-desc {
    margin: 0;
    font-size: 12px;
    color: var(--color-text-secondary, #909399);
  }
}

.pref-form {
  margin-top: 8px;

  .pref-tip {
    margin-left: 12px;
    font-size: 12px;
    color: var(--color-text-secondary, #909399);
  }
}
</style>
