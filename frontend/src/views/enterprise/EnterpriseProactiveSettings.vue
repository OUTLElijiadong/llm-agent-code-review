<template>
  <section class="proactive-settings" aria-labelledby="proactive-heading">
    <div class="heading"><h3 id="proactive-heading">小菱主动讨论</h3>
      <button type="button" @click="$emit('close')">关闭</button></div>
    <p v-if="loading" role="status">正在加载…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="error" type="button" data-action="refresh" :disabled="loading || saving" @click="load">重新读取</button>
    <form v-if="loaded" @submit.prevent="save">
      <label class="enabled"><input id="proactive-enabled" v-model="enabled" type="checkbox" :disabled="saving || ((!meteringReady || !workerReady) && !enabled)" /> 开启主动讨论</label>
      <label for="proactive-budget">每月预算（企业积分）</label>
      <input id="proactive-budget" v-model.number="monthlyCredits" type="number" min="0" max="100000000" step="1" :disabled="saving" />
      <p>公司群和小组群的消息可能发送给模型服务，费用从企业积分中扣除。</p>
      <p v-if="!meteringReady" role="status">平台计量尚未就绪。</p>
      <p v-if="!workerReady" role="status">主动讨论服务未开启。</p>
      <button type="submit" :disabled="saving || uncertain || !valid">{{ saving ? '保存中…' : '保存' }}</button>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { getPolicy, updatePolicy, type EnterpriseProactivePolicy } from '@/api/enterpriseProactive'
import { getToken } from '@/utils/token'

const props = defineProps<{ enterpriseId: number; currentUserId: number }>()
const emit = defineEmits<{ close: []; 'access-lost': [] }>()
const enabled = ref(false), monthlyCredits = ref(0), meteringReady = ref(false), workerReady = ref(false)
const loading = ref(false), saving = ref(false), loaded = ref(false), uncertain = ref(false), error = ref('')
const version = ref(0)
let generation = 0
let loadedToken: string | null = null
const valid = computed(() => Number.isInteger(monthlyCredits.value) && monthlyCredits.value >= 0 &&
  monthlyCredits.value <= 100000000 && (!enabled.value || (monthlyCredits.value > 0 && meteringReady.value && workerReady.value)))

function apply(policy: EnterpriseProactivePolicy) {
  enabled.value = policy.enabled; monthlyCredits.value = policy.monthly_credits; meteringReady.value = policy.metering_ready; workerReady.value = policy.worker_ready; version.value = policy.version
  loaded.value = true
}
function isCurrent(seq: number, enterprise: number, account: number, token: string | null) {
  return seq === generation && enterprise === props.enterpriseId && account === props.currentUserId && token === getToken()
}
function forbidden(cause: unknown) {
  const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
  return code === 403 || (code >= 40300 && code < 40400)
}
function conflict(cause: unknown) {
  const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
  return code === 409 || (code >= 40900 && code < 41000)
}
async function load() {
  const seq = ++generation, enterprise = props.enterpriseId, account = props.currentUserId, token = getToken()
  loading.value = true; loaded.value = false; error.value = ''
  try {
    const result = await getPolicy(enterprise, token)
    if (!isCurrent(seq, enterprise, account, token)) return
    loadedToken = token; apply(result); uncertain.value = false
  } catch (cause) {
    if (!isCurrent(seq, enterprise, account, token)) return
    error.value = forbidden(cause) ? '管理权限已变化，请刷新企业列表。' : '读取失败，请重试。'
    if (forbidden(cause)) emit('access-lost')
  } finally { if (seq === generation) loading.value = false }
}
async function save() {
  const enterprise = props.enterpriseId, account = props.currentUserId, token = getToken(), seq = generation
  if (!loaded.value || saving.value || uncertain.value || !valid.value) return
  if (token !== loadedToken) { loaded.value = false; error.value = '账号已变化，请重新读取。'; return }
  const input = { enabled: enabled.value, monthly_credits: monthlyCredits.value, expected_version: version.value }
  saving.value = true; error.value = ''
  try {
    const result = await updatePolicy(enterprise, input, token)
    if (isCurrent(seq, enterprise, account, token)) apply(result)
  } catch (cause) {
    if (!isCurrent(seq, enterprise, account, token)) return
    if (conflict(cause)) {
      await load()
      if (loaded.value && enterprise === props.enterpriseId && account === props.currentUserId && token === getToken()) {
        error.value = '设置已被修改，请核对后重新保存。'
      }
    }
    else if (forbidden(cause)) { error.value = '管理权限已变化，请刷新企业列表。'; emit('access-lost') }
    else { uncertain.value = true; error.value = '保存结果未知，请重新读取后核对。' }
  } finally { if (enterprise === props.enterpriseId && account === props.currentUserId && token === getToken()) saving.value = false }
}
watch(() => [props.enterpriseId, props.currentUserId], () => { saving.value = false; void load() }, { immediate: true })
onBeforeUnmount(() => { ++generation })
</script>

<style scoped>
.proactive-settings { display: grid; gap: .65rem; padding: .8rem; border: 1px solid #e5e7eb; border-radius: .65rem; background: #fff; }
.heading { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
h3, p { margin: 0; }
h3 { font-size: .95rem; }
form { display: grid; gap: .55rem; }
form p { color: #667085; font-size: .8rem; line-height: 1.5; }
.enabled { display: flex; align-items: center; gap: .35rem; }
input[type=number] { min-width: 0; width: 100%; padding: .45rem; border: 1px solid #d0d5dd; border-radius: .4rem; }
button { width: fit-content; }
.error { color: #b42318; font-size: .85rem; }
</style>
