<template>
  <div class="profile-center-page">
    <div class="page-header">
      <h2>个人中心</h2>
      <p class="page-sub">查看账户资料、管理头像与密码</p>
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

      <el-card shadow="hover" class="avatar-card">
        <h3 class="block-title">我的头像</h3>
        <div class="avatar-current">
          <UserAvatar :avatar="currentAvatar" :name="userStore.displayName || ''" :user-id="profile?.id || 0" :size="72" />
          <div class="avatar-meta">
            <p class="avatar-tip">挑一只喜欢的伙伴,或上传自己的图片(≤512KB,支持 PNG/JPEG/WebP/GIF)</p>
            <div class="avatar-buttons">
              <label class="upload-label">
                <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden @change="onUploadChange">
                <span role="button" class="upload-btn">上传图片</span>
              </label>
              <el-button v-if="currentAvatar" size="small" text type="danger" @click="onClearAvatar">恢复默认</el-button>
            </div>
          </div>
        </div>
        <div class="builtin-grid">
          <button
            v-for="item in BUILTIN_AVATARS"
            :key="item.key"
            type="button"
            class="builtin-item"
            :class="{ on: currentAvatar === `builtin:${item.key}` }"
            :title="item.label"
            @click="onPickBuiltin(item.key)"
          >
            <UserAvatar :avatar="`builtin:${item.key}`" :size="44" />
            <span>{{ item.label }}</span>
          </button>
        </div>
      </el-card>

      <el-card shadow="hover" class="action-card">
        <h3 class="block-title">账户操作</h3>
        <div class="action-row">
          <div>
            <p class="action-title">偏好设置(小菱)</p>
            <p class="action-desc">技术栈、关注方向与兴趣,小菱据此更懂你</p>
          </div>
          <el-button type="primary" plain @click="openPreferenceDialog">去设置</el-button>
        </div>
        <div class="action-row">
          <div>
            <p class="action-title">修改密码</p>
            <p class="action-desc">建议每 90 天更换一次密码</p>
          </div>
          <el-button type="primary" @click="goChangePassword">前往修改</el-button>
        </div>
        <div class="action-row">
          <div>
            <p class="action-title">API 配置</p>
            <p class="action-desc">配置个人大模型 API Key 和端点</p>
          </div>
          <el-button type="primary" plain @click="goApiConfig">前往配置</el-button>
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
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import dayjs from 'dayjs'
import { useUserStore } from '@/stores/user'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'

import { clearAvatar, setBuiltinAvatar, uploadAvatarImage } from '@/api/avatar'
import UserAvatar from '@/components/common/UserAvatar.vue'
import { BUILTIN_AVATARS, invalidateAvatarCache } from '@/constants/avatars'

const router = useRouter()
const userStore = useUserStore()

const profile = computed(() => userStore.profile)
const currentAvatar = ref('')

const roleLabel = computed(() => {
  switch (profile.value?.role) {
    case 'super_admin':
      return '超级管理员'
    case 'admin':
      return '管理员'
    case 'reviewer':
      return '审查员'
    default:
      return '普通用户'
  }
})

const roleTagType = computed<'danger' | 'warning' | 'primary'>(() => {
  if (profile.value?.role === 'admin' || profile.value?.role === 'super_admin') return 'danger'
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

/* ── 头像 ── */
function syncAvatarFromProfile(): void {
  currentAvatar.value = profile.value?.avatar || ''
}
syncAvatarFromProfile()

async function refreshProfile(): Promise<void> {
  await userStore.fetchProfile()
  syncAvatarFromProfile()
}

async function onPickBuiltin(key: string): Promise<void> {
  try {
    await setBuiltinAvatar(key)
    currentAvatar.value = `builtin:${key}`
    ElMessage.success('头像已更换')
    await refreshProfile()
  } catch { /* http 层已提示 */ }
}

async function onUploadChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 512 * 1024) {
    ElMessage.warning('图片不能超过 512KB')
    return
  }
  try {
    await uploadAvatarImage(file)
    invalidateAvatarCache(profile.value?.id)
    currentAvatar.value = 'upload'
    ElMessage.success('头像已上传')
    await refreshProfile()
  } catch { /* http 层已提示 */ }
}

async function onClearAvatar(): Promise<void> {
  try {
    await clearAvatar()
    invalidateAvatarCache(profile.value?.id)
    currentAvatar.value = ''
    ElMessage.success('已恢复默认头像')
    await refreshProfile()
  } catch { /* http 层已提示 */ }
}

function openPreferenceDialog(): void {
  window.dispatchEvent(new Event('prism:open-preference-dialog'))
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

function goApiConfig(): void {
  router.push('/profile/api-config')
}

async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确认退出登录?', '提示', { type: 'warning' })
    await userStore.logout()
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

/* ── 头像卡 ── */
.avatar-current {
  display: flex; gap: 16px; align-items: center; padding-bottom: 14px;
  border-bottom: 1px dashed var(--color-border-light, #ebeef5); margin-bottom: 14px;
}
.avatar-meta { flex: 1; }
.avatar-tip { margin: 0 0 8px; font-size: 12px; color: var(--color-text-secondary, #909399); line-height: 1.6; }
.avatar-buttons { display: flex; gap: 10px; align-items: center; }
.upload-label { cursor: pointer; }
.upload-btn {
  display: inline-block; padding: 6px 14px; border-radius: 999px; font-size: 12.5px;
  background: linear-gradient(135deg, var(--brand-500, #4078f4), var(--brand-600, #2f5ce0));
  color: #fff; font-weight: 600;
}
.builtin-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(76px, 1fr)); gap: 10px;
}
.builtin-item {
  display: grid; justify-items: center; gap: 5px; padding: 10px 4px 8px;
  border: 1.5px solid var(--color-border-light, #ebeef5); border-radius: 10px;
  background: #fff; cursor: pointer; font-size: 11.5px; color: var(--color-text-secondary, #909399);
  transition: all .16s ease;
  &:hover { border-color: var(--brand-300, #a8c4fa); transform: translateY(-2px); }
  &.on { border-color: var(--brand-500, #4078f4); background: var(--brand-50, #eef4ff); color: var(--brand-600, #2f5ce0); box-shadow: 0 4px 12px rgba(64,120,244,.14); }
}
@media (prefers-reduced-motion: reduce) {
  .builtin-item { transition: none; &:hover { transform: none; } }
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
