<template>
  <section class="management">
    <div class="heading"><h2>企业组织管理</h2><button :disabled="busy" @click="load">刷新</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="uncertain" class="warning" role="alert">上次管理操作的结果未知，请先刷新并核对成员、小组和邀请记录；如果邀请码没有显示，可撤销未确认的邀请。确认后再继续操作。</p>
    <button v-if="uncertain && !blocked" :disabled="busy || loading" @click="acknowledge">已核对结果，允许继续操作</button>
    <p v-if="loading" role="status">正在加载组织信息…</p>
    <template v-else-if="!blocked">
      <template v-if="isAdmin">
        <section><h3>邀请成员</h3><p class="muted">邀请口令只在生成时显示，请妥善转交。有效期为 24 小时。</p>
          <button data-action="invite" :disabled="disabled" @click="invite">生成邀请口令</button>
          <div v-if="invitationToken" class="token"><label for="invite-result">邀请口令</label><input id="invite-result" :value="invitationToken" readonly /></div>
          <ul><li v-for="inv in invitations" :key="inv.id">邀请 {{ inv.id }} · 已使用 {{ inv.use_count }} 次 · {{ inv.revoked_at ? '已撤销' : `有效期至 ${inv.expires_at}` }}
            <button v-if="!inv.revoked_at" :disabled="disabled" @click="revoke(inv.id)">撤销</button></li></ul>
        </section>
        <section><h3>企业成员</h3>
          <ul><li v-for="member in members" :key="member.user_id"><span>{{ name(member.user_id) }} · {{ roleName(member.role) }}</span>
            <span class="default-group">{{ defaultGroupLabel(member.default_group_id) }}</span>
            <button v-if="member.default_group_id !== null" :data-default-group-clear="member.user_id" :disabled="disabled"
              :aria-label="`清除${name(member.user_id)}的默认小组`" @click="setDefaultGroup(member.user_id, null)">清除默认小组</button>
            <select v-if="role === 'owner' && member.role !== 'owner'" :data-member-role="member.user_id" :value="member.role" :disabled="disabled" :aria-label="`${name(member.user_id)}的企业角色`" @change="changeCompanyRole(member, $event)"><option value="member">普通成员</option><option value="admin">企业管理员</option></select>
            <button v-if="member.role !== 'owner' && (role === 'owner' || member.role !== 'admin')" :disabled="disabled" @click="removeMember(member)">移除企业</button>
          </li></ul>
        </section>
        <section><h3>创建小组</h3><form data-action="create-group" @submit.prevent="createGroup">
          <label for="new-group-name">小组名称</label><input id="new-group-name" v-model.trim="groupName" maxlength="120" />
          <button type="submit" :disabled="disabled || !groupName">创建小组</button></form>
        </section>
      </template>
      <section><h3>管理小组</h3><p v-if="!managedGroups.length" class="muted">暂无可管理的小组。</p>
        <div class="actions"><button v-for="group in managedGroups" :key="group.id" :data-group="group.id" :aria-pressed="selectedGroup === group.id" @click="openGroup(group.id)">{{ group.name }}</button></div>
        <template v-if="selectedGroup">
          <p v-if="groupLoading" role="status">正在加载小组成员…</p>
          <ul v-else><li v-for="member in groupMembers" :key="member.user_id"><span>{{ name(member.user_id) }}</span>
            <select v-if="isAdmin" :data-group-role="member.user_id" :value="member.role" :disabled="disabled" :aria-label="`${name(member.user_id)}的小组角色`" @change="changeGroupRole(member, $event)"><option value="member">组员</option><option value="leader">组长</option></select>
            <span v-else>{{ member.role === 'leader' ? '组长' : '组员' }}</span>
            <button v-if="isAdmin && maySetDefaultGroup(member.user_id)" :data-default-group-set="member.user_id" :disabled="disabled"
              :aria-label="`将${name(member.user_id)}的默认小组设为当前小组`" @click="setDefaultGroup(member.user_id, selectedGroup)">设为默认小组</button>
            <button v-if="isAdmin || member.role === 'member'" :disabled="disabled" @click="removeGroupMember(member)">移出小组</button></li></ul>
          <form @submit.prevent="addGroupMember"><label for="group-user">添加企业成员</label><select id="group-user" v-model.number="newGroupUser"><option :value="0">选择成员</option><option v-for="person in availablePeople" :key="person.user_id" :value="person.user_id">{{ person.display_name }}</option></select><button type="submit" :disabled="disabled || !newGroupUser">添加</button></form>
          <button v-if="directoryMore" :disabled="disabled" @click="loadMoreDirectory">加载更多企业成员</button>
        </template>
      </section>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  createEnterpriseGroup, createEnterpriseInvitation, listEnterpriseDirectory, listEnterpriseGroupMembers,
  listEnterpriseGroups, listEnterpriseInvitations, listEnterpriseMembers, removeEnterpriseGroupMember,
  removeEnterpriseMember, revokeEnterpriseInvitation, setEnterpriseGroupMember, setEnterpriseMemberRole,
  setEnterpriseMemberDefaultGroup,
  type EnterpriseDirectoryMember, type EnterpriseGroup, type EnterpriseGroupMember, type EnterpriseInvitation,
  type EnterpriseMember,
} from '@/api/enterprise'
import { getToken } from '@/utils/token'
const props = defineProps<{ enterpriseId: number; currentUserId: number; role: 'owner' | 'admin' | 'member' }>()
const emit = defineEmits<{ (event: 'access-lost'): void; (event: 'changed'): void }>()
const groups = ref<EnterpriseGroup[]>([]), members = ref<EnterpriseMember[]>([]), directory = ref<EnterpriseDirectoryMember[]>([])
const invitations = ref<EnterpriseInvitation[]>([]), groupMembers = ref<EnterpriseGroupMember[]>([])
const invitationToken = ref(''), groupName = ref(''), error = ref('')
const selectedGroup = ref<number | null>(null), newGroupUser = ref(0)
const loading = ref(false), groupLoading = ref(false), busy = ref(false), blocked = ref(false), uncertain = ref(false)
const directoryMore = ref(false)
const isAdmin = computed(() => props.role === 'owner' || props.role === 'admin')
const managedGroups = computed(() => groups.value.filter(group => isAdmin.value || group.my_role === 'leader'))
const availablePeople = computed(() => directory.value.filter(person => !groupMembers.value.some(member => member.user_id === person.user_id)))
const disabled = computed(() => busy.value || loading.value || uncertain.value || blocked.value)
let generation = 0, groupGeneration = 0, operationGeneration = 0
let scopeToken = getToken(), scopeInvalid = false
const markerKey = (user: number, enterprise: number) => `prism:enterprise-management-uncertain:${user}:${enterprise}`
const name = (id: number) => directory.value.find(person => person.user_id === id)?.display_name || `成员 ${id}`
const roleName = (role: string) => ({ owner: '所有者', admin: '企业管理员', member: '普通成员' })[role] || role
const defaultGroupLabel = (id: number | null) => id === null ? '未设置默认小组'
  : `默认小组：${groups.value.find(group => group.id === id)?.name || `小组 ${id}`}`
function maySetDefaultGroup(userId: number) {
  const member = members.value.find(item => item.user_id === userId)
  return Boolean(member && selectedGroup.value && member.default_group_id !== selectedGroup.value)
}
function key() { return markerKey(props.currentUserId, props.enterpriseId) }
function forgetMarker(marker: string, request?: string) { try { if (!request || sessionStorage.getItem(marker) === request) sessionStorage.removeItem(marker) } catch { /* 本页状态仍保留 */ } }
function clear() {
  groups.value = []; members.value = []; directory.value = []; invitations.value = []; groupMembers.value = []
  invitationToken.value = ''; groupName.value = ''; selectedGroup.value = null; newGroupUser.value = 0
  directoryMore.value = false; groupLoading.value = false
}
function invalidateScope() {
  if (scopeInvalid) return
  scopeInvalid = true
  ++generation; ++groupGeneration; ++operationGeneration
  clear(); blocked.value = true; loading.value = false; busy.value = false
  error.value = '登录状态已变化，请重新进入企业空间。'
}
function isCurrent(current: number) {
  if (!scopeInvalid && scopeToken !== getToken()) invalidateScope()
  return !scopeInvalid && generation === current
}
function onStorage(event: StorageEvent) {
  if ((event.key === 'review_token' || event.key === null) && scopeToken !== getToken()) invalidateScope()
}
function onResume() {
  if (document.visibilityState === 'visible') isCurrent(generation)
}
function denied(cause: unknown): boolean {
  const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
  return (code >= 40300 && code < 40500) || code === 403 || code === 404
}
function fail(cause: unknown, message: string) {
  if (denied(cause)) {
    ++generation; ++groupGeneration; clear(); blocked.value = true; loading.value = false
    error.value = '组织管理权限已变化，请刷新企业列表。'; emit('access-lost')
  } else error.value = message
}
async function load() {
  if (!isCurrent(generation)) return
  const current = ++generation, id = props.enterpriseId, admin = isAdmin.value, token = scopeToken
  ++groupGeneration; loading.value = true; error.value = ''; groupMembers.value = []
  try {
    const [groupData, people, memberData, invitationData] = await Promise.all([
      listEnterpriseGroups(id, token), listEnterpriseDirectory(id, 0, token),
      admin ? listEnterpriseMembers(id, token) : Promise.resolve([]), admin ? listEnterpriseInvitations(id, token) : Promise.resolve([]),
    ])
    if (!isCurrent(current)) return
    groups.value = groupData; directory.value = people; directoryMore.value = people.length === 100
    members.value = memberData; invitations.value = invitationData
    if (!managedGroups.value.some(group => group.id === selectedGroup.value)) selectedGroup.value = null
    if (selectedGroup.value) await openGroup(selectedGroup.value)
  } catch (cause) { if (isCurrent(current)) fail(cause, '组织信息加载失败，请重试。') }
  finally { if (isCurrent(current)) loading.value = false }
}
async function openGroup(id: number) {
  if (!isCurrent(generation) || !managedGroups.value.some(group => group.id === id)) return
  const current = generation, request = ++groupGeneration, enterprise = props.enterpriseId, token = scopeToken
  selectedGroup.value = id; groupMembers.value = []; groupLoading.value = true; newGroupUser.value = 0
  try {
    const rows = await listEnterpriseGroupMembers(enterprise, id, token)
    if (isCurrent(current) && request === groupGeneration) groupMembers.value = rows
  } catch (cause) { if (isCurrent(current) && request === groupGeneration) fail(cause, '小组成员加载失败，请重试。') }
  finally { if (isCurrent(current) && request === groupGeneration) groupLoading.value = false }
}
async function loadMoreDirectory() {
  const current = generation, id = props.enterpriseId, token = scopeToken
  if (!isCurrent(current)) return
  try {
    const rows = await listEnterpriseDirectory(id, directory.value.length, token)
    if (!isCurrent(current)) return
    const seen = new Set(directory.value.map(person => person.user_id))
    directory.value.push(...rows.filter(person => !seen.has(person.user_id))); directoryMore.value = rows.length === 100
  } catch (cause) { if (isCurrent(current)) fail(cause, '加载企业成员失败，请重试。') }
}
async function perform<T>(action: () => Promise<T>, success?: (value: T) => void) {
  if (disabled.value || !isCurrent(generation)) return
  const current = generation, operation = ++operationGeneration, marker = key(), request = crypto.randomUUID()
  try { sessionStorage.setItem(marker, request) } catch { error.value = '浏览器存储不可用，无法安全记录操作状态。'; return }
  busy.value = true; error.value = ''
  try {
    const value = await action(); forgetMarker(marker, request)
    if (isCurrent(current)) { success?.(value); emit('changed'); await load() }
  } catch (cause) {
    if (denied(cause)) forgetMarker(marker, request)
    if (isCurrent(current)) {
      if (denied(cause)) {
        await load()
        if (!blocked.value && operation === operationGeneration) error.value = '操作被拒绝，请检查当前角色和目标成员权限。'
      } else {
        uncertain.value = true
        fail(cause, '操作结果未知，请先刷新核对，避免重复操作。')
      }
    }
  } finally { if (isCurrent(generation) && operation === operationGeneration) busy.value = false }
}
function acknowledge() {
  if (!window.confirm('请确认已刷新并核对上次操作的实际结果，再继续。')) return
  forgetMarker(key()); uncertain.value = false; error.value = ''
}
const invite = () => perform(() => createEnterpriseInvitation(props.enterpriseId), value => { invitationToken.value = value.token })
function setDefaultGroup(userId: number, groupId: number | null) {
  if (!isAdmin.value || !members.value.some(member => member.user_id === userId)) return
  if (groupId !== null && (groupId !== selectedGroup.value || !maySetDefaultGroup(userId)
      || !groupMembers.value.some(member => member.user_id === userId))) return
  const enterprise = props.enterpriseId, token = getToken()
  void perform(() => setEnterpriseMemberDefaultGroup(enterprise, userId, groupId, token))
}
function revoke(id: number) { if (window.confirm('确定撤销此邀请吗？')) void perform(() => revokeEnterpriseInvitation(props.enterpriseId, id)) }
function createGroup() {
  const value = groupName.value.trim(); if (!value) return
  void perform(() => createEnterpriseGroup(props.enterpriseId, value), () => { groupName.value = '' })
}
function changeCompanyRole(member: EnterpriseMember, event: Event) {
  const target = event.target as HTMLSelectElement, value = target.value as 'member' | 'admin'
  target.value = member.role
  if (value !== member.role) void perform(() => setEnterpriseMemberRole(props.enterpriseId, member.user_id, value))
}
function removeMember(member: EnterpriseMember) {
  if (window.confirm(`确定将 ${name(member.user_id)} 移出企业吗？`)) void perform(() => removeEnterpriseMember(props.enterpriseId, member.user_id))
}
function changeGroupRole(member: EnterpriseGroupMember, event: Event) {
  const target = event.target as HTMLSelectElement, value = target.value as 'member' | 'leader', group = selectedGroup.value
  target.value = member.role
  if (group && value !== member.role) void perform(() => setEnterpriseGroupMember(props.enterpriseId, group, member.user_id, value))
}
function removeGroupMember(member: EnterpriseGroupMember) {
  const group = selectedGroup.value
  if (group && window.confirm(`确定将 ${name(member.user_id)} 移出小组吗？`)) void perform(() => removeEnterpriseGroupMember(props.enterpriseId, group, member.user_id))
}
function addGroupMember() {
  const group = selectedGroup.value, user = newGroupUser.value
  if (group && user) void perform(() => setEnterpriseGroupMember(props.enterpriseId, group, user, 'member'), () => { newGroupUser.value = 0 })
}
watch(() => [props.enterpriseId, props.currentUserId, props.role] as const, () => {
  ++generation; ++groupGeneration; ++operationGeneration; clear(); blocked.value = false; busy.value = false; error.value = ''
  scopeToken = getToken(); scopeInvalid = false
  try { uncertain.value = sessionStorage.getItem(key()) !== null } catch { uncertain.value = true }
  void load()
}, { immediate: true, flush: 'sync' })
onMounted(() => {
  window.addEventListener('storage', onStorage)
  window.addEventListener('prism:auth-expired', invalidateScope)
  window.addEventListener('focus', onResume)
  document.addEventListener('visibilitychange', onResume)
})
onBeforeUnmount(() => {
  ++generation; ++groupGeneration; ++operationGeneration
  window.removeEventListener('storage', onStorage)
  window.removeEventListener('prism:auth-expired', invalidateScope)
  window.removeEventListener('focus', onResume)
  document.removeEventListener('visibilitychange', onResume)
})
</script>
<style scoped>
.management { background: white; border: 1px solid #e9eaf3; border-radius: 14px; padding: 20px; }
.heading, .actions, li, form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.heading { justify-content: space-between; } section section { padding: 16px 0; border-bottom: 1px solid #edf0f6; }
h2 { font-size: 17px; margin: 0; } h3 { font-size: 15px; margin: 0 0 12px; } ul { padding: 0; list-style: none; }
li { padding: 5px 0; } button, select, input { font: inherit; border: 1px solid #d9ddeb; background: #fff; border-radius: 8px; padding: 8px 10px; color: inherit; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }.muted { color: #69718a; font-size: 13px; }
.error { color: #a23434; }.warning { color: #8e5a15; }.default-group { color: #69718a; font-size: 13px; }.token { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }.token input { flex: 1; min-width: 0; }
</style>
