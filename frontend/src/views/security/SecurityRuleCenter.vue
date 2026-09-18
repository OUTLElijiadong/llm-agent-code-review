<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Collection, Lock, Setting } from '@element-plus/icons-vue'

import { useUserStore } from '@/stores/user'
import RuleConfig from '@/views/rule/RuleConfig.vue'
import SecurityCenter from './SecurityCenter.vue'
import SecurityRuleCatalog from './SecurityRuleCatalog.vue'

type CenterTab = 'posture' | 'rules' | 'catalog'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const canViewSecurity = computed(() => userStore.hasPermission('security:view'))
const canViewRules = computed(() => userStore.hasPermission('rule:view'))
const activeTab = computed<CenterTab>({
  get() {
    if (route.query.tab === 'rules' && canViewRules.value) return 'rules'
    if (route.query.tab === 'catalog' && canViewSecurity.value) return 'catalog'
    return canViewSecurity.value ? 'posture' : 'rules'
  },
  set(value) {
    const query = { ...route.query }
    if (value === 'rules' || value === 'catalog') query.tab = value
    else delete query.tab
    void router.push({ path: '/security', query })
  },
})
</script>

<template>
  <div class="security-rule-center">
    <header class="center-header"><h1>安全与审查规则</h1></header>

    <el-tabs v-model="activeTab" class="center-tabs">
      <el-tab-pane v-if="canViewSecurity" name="posture" lazy>
        <template #label>
          <span class="tab-label"><el-icon><Lock /></el-icon>安全态势与规则基座</span>
        </template>
        <SecurityCenter embedded />
      </el-tab-pane>
      <el-tab-pane v-if="canViewRules" name="rules" lazy>
        <template #label>
          <span class="tab-label"><el-icon><Setting /></el-icon>审查规则管理</span>
        </template>
        <RuleConfig embedded />
      </el-tab-pane>
      <el-tab-pane v-if="canViewSecurity" name="catalog" lazy>
        <template #label>
          <span class="tab-label"><el-icon><Collection /></el-icon>统一规则目录</span>
        </template>
        <SecurityRuleCatalog />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped lang="scss">
.security-rule-center {
  display: grid;
  gap: 12px;
}

.center-header {
  h1 {
    margin: 0;
    font-size: 22px;
    line-height: 1.25;
  }
}

.center-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 18px;
  }
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

</style>
