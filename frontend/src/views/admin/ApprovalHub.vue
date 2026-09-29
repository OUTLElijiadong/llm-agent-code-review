<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { APPROVAL_TYPES, resolveApprovalType, type ApprovalType } from '@/utils/adminApprovalType'

const AgentReleaseAdmin = defineAsyncComponent(() => import('./AgentReleaseAdmin.vue'))
const ApprovalCenter = defineAsyncComponent(() => import('./ApprovalCenter.vue'))
const EvolutionCenter = defineAsyncComponent(() => import('./EvolutionCenter.vue'))

const route = useRoute()
const router = useRouter()
const activeType = computed<ApprovalType>(() => resolveApprovalType(route.query.section, route.query.approvalType))

const activeComponent = computed(() => ({
  'agent-release': AgentReleaseAdmin,
  execution: ApprovalCenter,
  rules: EvolutionCenter,
}[activeType.value]))
const activeComponentProps = computed(() => activeType.value === 'rules' ? { approvalOnly: true } : {})

function selectType(value: string | number): void {
  void router.replace({
    path: route.path,
    query: { ...route.query, section: 'approvals', approvalType: String(value) },
  })
}
</script>

<template>
  <section class="approval-hub" aria-label="审批中心">
    <header class="approval-hub-head">
      <p>在此查看 Agent 发布、执行审批和规则提案。规则提案仍经过独立的黄金集评估闸门。</p>
      <el-tabs :model-value="activeType" @tab-change="selectType">
        <el-tab-pane v-for="item in APPROVAL_TYPES" :key="item.name" :name="item.name" :label="item.label" />
      </el-tabs>
    </header>
    <component :is="activeComponent" v-bind="activeComponentProps" />
  </section>
</template>

<style scoped>
.approval-hub { min-width: 0; }
.approval-hub-head { margin-bottom: 14px; }
.approval-hub-head p { margin: 0 0 8px; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
.approval-hub-head :deep(.el-tabs__header) { margin-bottom: 0; }
</style>
