<script setup lang="ts">
/**
 * 小菱迎宾组件(用户端全局挂载):
 * 1) 新手引导:注册后首次登录弹一次(后端 first_login 标志驱动,常用用户不弹)。
 * 2) 偏好询问:非管理员且未做过偏好设置时温和询问一次;"稍后再说"七天内不主动提醒。
 * 3) 监听 prism:open-preference-dialog 事件(用户菜单"偏好设置"入口)随时重开。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { getProfile } from '@/api/profile'
import XiaolingOnboardingDialog from '@/components/ai/XiaolingOnboardingDialog.vue'
import XiaolingPreferenceDialog from '@/components/ai/XiaolingPreferenceDialog.vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const onboardingVisible = ref(false)
const preferenceVisible = ref(false)
let preferenceLaterKey = ''
let onboardingKey = ''
let mounted = true

function finishOnboarding() {
  try { if (onboardingKey) sessionStorage.setItem(onboardingKey, 'done') } catch { /* 配额忽略 */ }
}

function preferenceClosedByLater() {
  try { if (preferenceLaterKey) localStorage.setItem(preferenceLaterKey, String(Date.now())) } catch { /* 配额忽略 */ }
}

function openPreferenceDialog() {
  preferenceVisible.value = true
}

async function evaluateTriggers() {
  const user = userStore.profile
  if (!user?.id) return
  onboardingKey = `prism-onboarding-shown:${user.id}`
  preferenceLaterKey = `prism-pref-later:${user.id}`

  try {
    if (sessionStorage.getItem(onboardingKey) === 'pending') {
      onboardingVisible.value = true
      return // 引导优先;偏好询问等下一次进入(引导关闭即置 done)
    }
  } catch { /* ignore */ }

  if (userStore.isAdmin()) return
  try {
    const last = Number(localStorage.getItem(preferenceLaterKey) || 0)
    if (Date.now() - last < 7 * 86400_000) return
  } catch { /* ignore */ }
  try {
    const profile = await getProfile()
    if (!mounted || userStore.profile?.id !== user.id || userStore.isAdmin()) return
    const hasPreferences = [profile.hobbies, profile.goals, profile.tech_stack, profile.preferred_language, profile.experience_level].some(value => value?.trim()) || profile.focus_areas.length > 0
    if (!profile.preference_prompted && !hasPreferences && profile.should_prompt !== false) preferenceVisible.value = true
  } catch { /* 画像接口失败不弹,下次再问 */ }
}

function onOpenPreference() {
  openPreferenceDialog()
}

onMounted(() => {
  void evaluateTriggers()
  window.addEventListener('prism:open-preference-dialog', onOpenPreference)
})

onBeforeUnmount(() => {
  mounted = false
  window.removeEventListener('prism:open-preference-dialog', onOpenPreference)
})
</script>

<template>
  <XiaolingOnboardingDialog v-model="onboardingVisible" @finished="finishOnboarding" />
  <XiaolingPreferenceDialog v-model="preferenceVisible" @closed="preferenceClosedByLater" />
</template>
