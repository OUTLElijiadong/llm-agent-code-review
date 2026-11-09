<template>
  <section class="approval-page" aria-labelledby="approval-title">
    <header class="approval-header">
      <div>
        <h2 id="approval-title">小菱操作审批</h2>
        <p>这里只展示由小菱提出、尚未发布的企业公告。批准前请核对标题和完整正文。</p>
      </div>
      <button type="button" :disabled="loading" @click="loadApprovals">{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>

    <p v-if="error" class="approval-error" role="alert">{{ error }}</p>
    <p v-if="loading && !items.length" class="approval-state" role="status">正在读取审批…</p>
    <div v-else-if="!loading && !items.length && !error" class="approval-empty">
      <span aria-hidden="true">✓</span>
      <h3>当前没有待审批请求</h3>
      <p>小菱提出的公告会显示在这里，只有企业所有者或管理员可以处理。</p>
    </div>

    <ol v-else-if="items.length" class="approval-list">
      <li v-for="item in items" :key="item.id" class="approval-card">
        <div class="approval-meta">
          <span>公告发布申请 · {{ item.group_id ? '来自小组会话' : '来自企业会话' }}</span>
          <time v-if="item.create_time" :datetime="item.create_time">{{ formatTime(item.create_time) }}</time>
        </div>
        <h3>{{ item.title || '（无标题）' }}</h3>
        <p class="approval-content">{{ item.content || '（无正文）' }}</p>
        <p class="approval-requester">申请成员编号：{{ item.initiator_user_id }} · 运行 {{ item.run_id }}</p>
        <div class="approval-actions">
          <button type="button" class="reject" :disabled="busyId !== null" @click="decide(item, 'reject')">拒绝</button>
          <button type="button" class="approve" :disabled="busyId !== null" @click="decide(item, 'approve')">
            {{ busyId === item.id ? '提交中…' : '批准并发布' }}
          </button>
          <button type="button" class="open-room" :disabled="busyId !== null" @click="emit('open-conversation', item.conversation_id)">查看会话</button>
        </div>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import {
  decideEnterpriseAssistantApproval, listEnterpriseAssistantApprovals,
  type EnterpriseAssistantApproval,
} from '@/api/enterprise'
import { getToken } from '@/utils/token'

const props = defineProps<{ enterpriseId: number }>()
const emit = defineEmits<{ 'open-conversation': [conversationId: number] }>()
const items = ref<EnterpriseAssistantApproval[]>([])
const loading = ref(false)
const busyId = ref<number | null>(null)
const error = ref('')
let generation = 0

async function loadApprovals() {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const result = await listEnterpriseAssistantApprovals(props.enterpriseId)
    if (current === generation) items.value = result.items
  } catch {
    if (current === generation) error.value = '审批列表读取失败；请确认企业管理员权限后刷新。'
  } finally {
    if (current === generation) loading.value = false
  }
}

async function decide(item: EnterpriseAssistantApproval, decision: 'approve' | 'reject') {
  const token = getToken()
  const current = generation
  busyId.value = item.id
  error.value = ''
  try {
    await decideEnterpriseAssistantApproval(props.enterpriseId, item.id, decision,
      decision === 'reject' ? '企业管理员拒绝发布此公告' : '', token)
    if (current !== generation) return
    items.value = items.value.filter(row => row.id !== item.id)
    emit('open-conversation', item.conversation_id)
  } catch {
    if (current === generation) error.value = '审批未完成；刷新列表确认服务器状态后再操作。'
  } finally {
    if (current === generation) busyId.value = null
  }
}

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

watch(() => props.enterpriseId, () => { items.value = []; void loadApprovals() })
onMounted(() => { void loadApprovals() })
</script>

<style scoped>
.approval-page { min-height: 360px; padding: 22px; border: 1px solid #e4e7ed; border-radius: 14px; background: #fff; color: #252a3a; }
.approval-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding-bottom: 16px; border-bottom: 1px solid #eceef2; }
.approval-header h2 { margin: 0; font-size: 19px; }.approval-header p, .approval-empty p { margin: 7px 0 0; color: #777f91; font-size: 13px; line-height: 1.6; }
button { padding: 8px 12px; border: 1px solid #d9ddeb; border-radius: 8px; background: #fff; color: inherit; font: inherit; cursor: pointer; }
button:disabled { opacity: .55; cursor: default; }.approval-state, .approval-error { padding: 18px; color: #69718a; }.approval-error { color: #a23f3f; }
.approval-empty { display: grid; justify-items: center; padding: 70px 20px; text-align: center; }.approval-empty > span { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 50%; background: #e9f7ef; color: #33845c; font-size: 22px; }.approval-empty h3 { margin: 14px 0 0; font-size: 16px; }
.approval-list { display: grid; gap: 14px; margin: 18px 0 0; padding: 0; list-style: none; }.approval-card { padding: 18px; border: 1px solid #e7e9ef; border-radius: 12px; background: #fbfcfe; }
.approval-meta { display: flex; justify-content: space-between; gap: 12px; color: #787f90; font-size: 12px; }.approval-card h3 { margin: 13px 0 8px; font-size: 16px; }.approval-content { max-height: 260px; overflow: auto; margin: 0; padding: 12px; border: 1px solid #e9ebf1; border-radius: 8px; background: #fff; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
.approval-requester { color: #8a91a0; font-size: 12px; }.approval-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }.approval-actions .approve { border-color: #c6ddcf; background: #edf8f1; color: #286843; }.approval-actions .reject { border-color: #ead0d0; background: #fff4f4; color: #9b4a4a; }
@media (max-width: 600px) { .approval-page { padding: 15px; }.approval-meta { flex-direction: column; gap: 4px; }.approval-actions { justify-content: stretch; }.approval-actions button { flex: 1; } }
</style>
