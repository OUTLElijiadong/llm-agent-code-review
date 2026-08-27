<script setup lang="ts">
/**
 * Agent 回复中的站内页面引导链接。
 *
 * 安全边界:只有命中应用路由表、存在匹配路由记录且当前用户鉴权信息
 * 可放行守卫规则时才渲染为可点击卡片并执行 router.push;
 * 外部链接与无权路由一律渲染为纯文本,从界面上隐藏,防止模型编造
 * 路由形成越权入口或钓鱼跳转。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { TopRight } from '@element-plus/icons-vue'

import { useUserStore } from '@/stores/user'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import { requestXiaolingNavigation } from '@/utils/xiaolingNavigation'

const props = defineProps<{
  href: string
  label: string
  hint?: string
  /** 显式指令导航(模型发出的"带我去"按钮)时加粗展示 */
  prominent?: boolean
}>()

const router = useRouter()
const userStore = useUserStore()

const resolved = computed(() => {
  if (!props.href.startsWith('/') || props.href.startsWith('//')) return null
  try {
    const match = router.resolve({ path: props.href })
    return match.matched.length ? match : null
  } catch {
    return null
  }
})

const allowed = computed(() => {
  if (!resolved.value) return false
  return isNavigationPathAllowed(router, resolved.value.fullPath, userStore)
})

function navigate(event: MouseEvent): void {
  if (!allowed.value || !resolved.value) return
  const fullPath = resolved.value.fullPath
  requestXiaolingNavigation(
    fullPath,
    props.label,
    () => { void router.push(fullPath) },
    event.currentTarget as HTMLElement | null,
  )
}
</script>

<template>
  <button
    v-if="allowed"
    type="button"
    class="agent-nav-link"
    :class="{ 'is-prominent': prominent }"
    :title="hint || `前往${label}`"
    @click="navigate"
  >
    <span class="agent-nav-text">{{ label }}</span>
    <el-icon class="agent-nav-icon"><TopRight /></el-icon>
  </button>
</template>

<style scoped>
.agent-nav-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 2px;
  padding: 2px 10px;
  border: 1px solid var(--brand-300, #b7bcf5);
  border-radius: 999px;
  background: var(--brand-50, #f5f6ff);
  color: var(--brand-600, #5b58e8);
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.6;
  cursor: pointer;
  transition: all 0.16s ease;
  vertical-align: baseline;
}

.agent-nav-link:hover {
  background: var(--brand-500, #5b58e8);
  border-color: var(--brand-500, #5b58e8);
  color: #fff;
}

.agent-nav-link.is-prominent {
  display: flex;
  width: 100%;
  justify-content: space-between;
  margin-top: 10px;
  padding: 8px 14px;
  font-size: 13px;
  border-radius: 8px;
}

.agent-nav-icon {
  font-size: 13px;
}

</style>
