<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const SecurityCenter = defineAsyncComponent(() => import('./SecurityCenter.vue'))
const RuleConfig = defineAsyncComponent(() => import('@/views/rule/RuleConfig.vue'))

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

type Section = 'posture' | 'rules'
const canPosture = computed(() => userStore.hasPermission('security:view') || userStore.isAdmin())
const canRules = computed(() => userStore.hasPermission('rule:view') || userStore.isAdmin())
const section = computed<Section>(() => {
  if (route.query.section === 'rules' && canRules.value) return 'rules'
  if (canPosture.value) return 'posture'
  return 'rules'
})

function selectSection(value: string | number): void {
  const nextSection: Section = value === 'rules' ? 'rules' : 'posture'
  void router.replace({ path: '/security', query: nextSection === 'posture' ? {} : { section: nextSection } })
}
</script>

<template>
  <div class="security-review-page">
    <header class="security-review-head">
      <div>
        <h2>安全与审查规则</h2>
        <p>安全态势、扫描入口和审查规则使用同一安全事实源。</p>
      </div>
      <el-tabs :model-value="section" @tab-change="selectSection">
        <el-tab-pane v-if="canPosture" label="安全态势" name="posture" />
        <el-tab-pane v-if="canRules" label="审查规则" name="rules" />
      </el-tabs>
    </header>
    <SecurityCenter v-if="section === 'posture' && canPosture" />
    <RuleConfig v-else-if="canRules" />
    <el-empty v-else description="当前账号没有可访问的安全功能" />
  </div>
</template>

<style scoped>
.security-review-page { min-width: 0; }
.security-review-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.security-review-head h2 { margin: 0; }
.security-review-head p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 760px) {
  .security-review-head { align-items: stretch; flex-direction: column; gap: 8px; }
}
</style>
