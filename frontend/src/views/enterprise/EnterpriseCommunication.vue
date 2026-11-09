<template>
  <main ref="enterprisePageRef" class="enterprise-page">
    <header class="page-header">
      <h1 ref="enterpriseHeadingRef" tabindex="-1">企业空间</h1>
      <div class="header-actions">
        <button type="button" data-action="join-enterprise" @click="showInvitation = !showInvitation">加入企业</button>
        <button type="button" :disabled="loadingCompanies" @click="loadCompanies">刷新企业</button>
      </div>
    </header>
    <p v-if="loadingCompanies && !companies.length" class="notice" role="status">正在加载企业…</p>
    <p v-if="pageError" class="notice error" role="alert">{{ pageError }}</p>
    <section v-if="showInvitation || (!loadingCompanies && !companies.length && !pageError)" class="card empty">
      <h2>{{ companies.length ? '加入其他企业' : '还没有加入企业' }}</h2>
      <p>如果收到企业管理员的邀请码，可在这里加入。</p>
      <form class="join" @submit.prevent="joinEnterprise">
        <label for="invite-token">邀请口令</label>
        <input id="invite-token" v-model.trim="invitation" autocomplete="off" placeholder="输入邀请口令" />
        <button type="submit" :disabled="joining || !invitation">{{ joining ? '加入中…' : '加入企业' }}</button>
      </form>
    </section>
    <template v-if="companies.length">
      <div class="workspace-top">
        <label for="company-select">当前企业</label>
        <select id="company-select" :value="companyId ?? ''" @change="chooseCompany(Number(($event.target as HTMLSelectElement).value))">
          <option v-for="company in companies" :key="company.id" :value="company.id">{{ company.name }}</option>
        </select>
      </div>
      <p v-if="chatError && !selectedRoom" class="notice error" role="alert">{{ chatError }}</p>
      <nav class="workspace-tabs" aria-label="企业功能">
        <button type="button" :aria-pressed="activeSection === 'chat'" @click="switchSection('chat')">聊天</button>
        <button type="button" :aria-pressed="activeSection === 'forum'" @click="switchSection('forum')">企业论坛</button>
        <button type="button" :aria-pressed="activeSection === 'announcement'" @click="switchSection('announcement')">企业公告</button>
        <button type="button" :aria-pressed="activeSection === 'knowledge'" @click="switchSection('knowledge')">企业知识库</button>
        <button type="button" :aria-pressed="activeSection === 'assets'" @click="switchSection('assets')">Agent 与 Skill</button>
        <button type="button" :aria-pressed="activeSection === 'remote-tests'" @click="switchSection('remote-tests')">远测申请与审批</button>
        <button v-if="canManageCompany" type="button" :aria-pressed="activeSection === 'assistant-approvals'" @click="switchSection('assistant-approvals')">小菱审批</button>
        <button v-if="canManageGroups" type="button" :aria-pressed="activeSection === 'management'" @click="switchSection('management')">组织管理</button>
        <button v-if="canManageGroups" type="button" :aria-pressed="activeSection === 'usage'" @click="switchSection('usage')">企业用量</button>
      </nav>
      <div v-if="activeSection === 'chat' || chatFloating" class="chat-shell" :class="{ 'has-room': Boolean(selectedRoom), 'floating-only': activeSection !== 'chat' }">
        <aside class="conversation-list" aria-label="企业会话列表">
          <div class="conversation-list-head">
            <div class="conversation-list-title"><span class="enterprise-avatar">{{ companyInitial }}</span><div><h2>{{ currentCompanyName }}</h2><p class="enterprise-meta">{{ currentCompanyMeta }}</p></div></div>
            <button class="icon-button" type="button" aria-label="刷新会话" title="刷新会话" @click="loadCompanies">↻</button>
          </div>
          <button v-if="canManageCompany && currentUserId" type="button" class="proactive-toggle"
            :aria-expanded="proactiveSettingsOpen" @click="proactiveSettingsOpen = !proactiveSettingsOpen">小菱主动讨论设置</button>
          <EnterpriseProactiveSettings v-if="proactiveSettingsOpen && canManageCompany && companyId && currentUserId"
            :key="`${companyId}:${currentUserId}`" :enterprise-id="companyId" :current-user-id="currentUserId"
            @close="proactiveSettingsOpen = false" @access-lost="clearEnterpriseAccess" />
          <label class="conversation-search"><span aria-hidden="true">⌕</span><input v-model.trim="roomFilter" type="search" placeholder="搜索会话" aria-label="搜索会话" /></label>
          <div class="conversation-quick-actions">
            <button type="button" @click="openCompany"><span class="room-avatar company-avatar" aria-hidden="true" /><span>公司群</span><span class="room-arrow" aria-hidden="true" /></button>
            <button v-for="group in chatGroups" :key="group.id" type="button" @click="openGroup(group)"><span class="room-avatar group-avatar" aria-hidden="true" /><span class="truncate">{{ group.name }}</span><span class="room-arrow" aria-hidden="true" /></button>
          </div>
          <div class="conversation-divider"><span>最近会话</span><button v-if="directoryHasMore" type="button" @click="loadMoreMembers">更多成员</button></div>
          <button v-if="!filteredRooms.length" type="button" class="conversation-empty" @click="handleEmptyRoomsAction">
            {{ roomFilter ? '没有匹配的会话，清除搜索' : '暂无会话，打开公司群开始沟通' }}
          </button>
          <button v-for="room in filteredRooms" :key="room.id" type="button" class="room-row" :class="{ selected: room.id === roomId }" @click="selectRoom(room.id)">
            <span class="room-avatar" :class="`avatar-${room.kind}`">{{ roomAvatar(room) }}</span>
            <span class="room-copy"><span class="room-title">{{ roomTitle(room) }}</span><span class="room-preview">{{ roomKindLabel(room) }}</span></span>
            <span class="room-kind">{{ roomKindShort(room) }}</span>
          </button>
          <form class="direct-start" @submit.prevent="openDirect">
            <label for="direct-member">发起同事私聊</label>
            <div><select id="direct-member" v-model.number="directUserId"><option :value="0">选择企业成员</option><option v-for="member in otherMembers" :key="member.user_id" :value="member.user_id">{{ member.display_name }}</option></select><button type="submit" :disabled="!directUserId" aria-label="打开私聊">开始</button></div>
          </form>
        </aside>
        <div class="conversation-stage">
          <Teleport to="body" :disabled="!chatFloating">
            <section
              ref="chatPanelRef"
              class="chat-pane chat"
              :class="{ 'is-floating': chatFloating, 'is-dragging': chatDragging, 'is-resizing': chatResizing }"
              :style="chatPanelStyle"
              :role="chatFloating ? 'dialog' : 'region'"
              :aria-label="selectedRoom ? `${roomTitle(selectedRoom)}会话` : '企业聊天'"
              tabindex="-1"
              @pointermove="moveChatWindow"
              @pointerup="endChatWindowMove"
              @pointercancel="endChatWindowMove"
              @lostpointercapture="endChatWindowMove"
            >
              <template v-if="selectedRoom">
                <header class="chat-pane-header">
                  <button class="mobile-back" type="button" aria-label="返回会话列表" @click="backToConversations">‹</button>
                  <button class="chat-drag-handle" type="button" aria-label="移动聊天窗口，可使用方向键移动"
                    title="拖动或使用方向键移动聊天窗口" @pointerdown="beginChatWindowMove" @keydown="moveChatWindowByKeyboard">⠿</button>
                  <span class="room-avatar header-avatar" :class="`avatar-${selectedRoom.kind}`">{{ roomAvatar(selectedRoom) }}</span>
                  <div class="chat-title-block"><h2>{{ roomTitle(selectedRoom) }}</h2><p>{{ roomKindLabel(selectedRoom) }}<template v-if="selectedRoom.kind === 'company'"> · 企业成员可见</template><template v-else-if="selectedRoom.kind === 'group'"> · 本小组成员可见</template></p></div>
                  <div class="chat-toolbar">
                    <button class="icon-button" type="button" aria-label="刷新消息" title="刷新消息" @click="refreshMessages"><span class="refresh-icon" aria-hidden="true" /><span class="sr-only">刷新消息</span></button>
                    <button class="team-progress-toggle" type="button" :aria-pressed="teamProgressOpen"
                      @click="teamProgressOpen = !teamProgressOpen">{{ teamProgressOpen ? '收起团队' : '团队进度' }}</button>
                    <button class="icon-button floating-toggle" type="button" :aria-label="chatFloating ? '收回聊天窗口' : '弹出悬浮聊天窗口'" :title="chatFloating ? '收回聊天窗口' : '弹出悬浮聊天窗口'" @click="toggleChatWindow">{{ chatFloating ? '▣' : '□' }}</button>
                  </div>
                </header>
                <div v-if="chatError" class="chat-inline-error" role="alert">
                  <span>{{ chatError }}</span>
                  <button v-if="assistantRetry && assistantRetry.conversationId === roomId" type="button"
                    :disabled="sending" @click="retryAssistant">检查后重试小菱</button>
                </div>
                <div ref="messagesContainer" class="message-list" aria-live="polite" aria-relevant="additions text">
                  <button v-if="hasMore" class="load-older" type="button" :disabled="loadingMessages" @click="loadOlderMessages">{{ loadingMessages ? '加载中…' : '加载更多消息' }}</button>
                  <p v-if="loadingMessages && !messages.length" class="message-empty">正在加载消息…</p>
                  <p v-else-if="!messages.length" class="message-empty">这里还没有消息，发一条消息开始讨论。</p>
                  <article v-for="message in messages" :key="message.id" class="message message-row" :class="{ mine: message.sender_type !== 'assistant' && message.sender_id === currentUserId, assistant: message.sender_type === 'assistant' }">
                    <span v-if="message.sender_type === 'assistant'" class="message-avatar assistant-avatar" aria-label="小菱">菱</span>
                    <span v-else-if="message.sender_id !== currentUserId" class="message-avatar">{{ memberName(message.sender_id).slice(0, 1) }}</span>
                    <div class="message-body"><span v-if="message.sender_type === 'assistant'" class="sender">小菱 · 企业助手</span><span v-else-if="message.sender_id !== currentUserId" class="sender">{{ memberName(message.sender_id) }}</span><p class="message-bubble">{{ message.content }}</p><time v-if="message.create_time" class="message-time" :datetime="message.create_time">{{ formatMessageTime(message.create_time) }}</time></div>
                    <span v-if="message.sender_type !== 'assistant' && message.sender_id === currentUserId" class="message-avatar mine-avatar">{{ currentUserName.slice(0, 1) }}</span>
                  </article>
                </div>
                <EnterpriseAgentTeamProgress
                  v-if="teamProgressOpen && companyId && roomId && currentUserId && selectedRoom"
                  :key="`${companyId}:${roomId}:${currentUserId}`"
                  :enterprise-id="companyId"
                  :conversation-id="roomId"
                  :current-user-id="currentUserId"
                  :room-kind="selectedRoom.kind"
                  :can-manage-company="canManageCompany"
                  :can-manage-group="canManageSelectedGroup"
                  @close="teamProgressOpen = false"
                />
                <section v-if="assistantRun && activeAssistantRoom" class="assistant-run-card" aria-live="polite">
                  <div class="assistant-run-heading">
                    <strong>{{ assistantStatusLabel(assistantRun.status) }}</strong>
                    <span v-if="assistantRun.recoverable">可恢复</span>
                  </div>
                  <p v-if="assistantRun.status === 'waiting_approval'">公告尚未发布，需企业所有者或管理员检查完整内容并审批。</p>
                  <section v-if="assistantRun.status === 'awaiting_clarification'" class="assistant-clarification" aria-label="小菱权限确认">
                    <p v-if="assistantClarificationLoading">正在核对权限意见…</p>
                    <template v-else-if="assistantClarification">
                      <p class="assistant-clarification-action">
                        {{ clarificationActionLabel(assistantClarification.action) }}<template v-if="assistantClarification.title">：{{ assistantClarification.title }}</template>
                      </p>
                      <article v-for="candidate in assistantClarification.candidates" :key="candidate.message_id"
                        class="clarification-candidate">
                        <blockquote>{{ candidate.content }}</blockquote>
                        <div class="clarification-candidate-meta">
                          <span>{{ memberName(candidate.author_id) }}</span>
                          <span>{{ clarificationDecisionLabel(candidate.decision) }}</span>
                        </div>
                        <div v-if="candidate.can_decide" class="clarification-actions">
                          <button type="button" :disabled="assistantActionLoading"
                            @click="decideAssistantClarification(candidate.message_id, 'allow')">允许本次</button>
                          <button type="button" :disabled="assistantActionLoading"
                            @click="decideAssistantClarification(candidate.message_id, 'deny')">阻止本次</button>
                          <button type="button" :disabled="assistantActionLoading"
                            @click="decideAssistantClarification(candidate.message_id, 'unrelated')">与本次无关</button>
                        </div>
                      </article>
                      <div v-if="assistantClarification.can_resume" class="clarification-actions">
                        <button type="button" class="clarification-resume" :disabled="assistantActionLoading"
                          @click="resumeAssistantClarification">继续执行</button>
                      </div>
                      <p v-else-if="assistantClarification.status === 'denied'" class="clarification-blocked">
                        当前决定阻止本次动作；来源作者可修订，更高权限成员可复核。
                      </p>
                      <p v-else-if="assistantClarification.status === 'complete'" class="clarification-waiting">
                        权限意见已齐，等待本次发起人继续。
                      </p>
                      <p v-else-if="['stale', 'expired', 'cancelled'].includes(assistantClarification.status)"
                        class="clarification-blocked">
                        权限或指令已变化，本次不能继续。
                      </p>
                    </template>
                    <p v-else>澄清状态暂不可用，请刷新后重试。</p>
                  </section>
                  <form v-if="assistantRun.status === 'waiting_input' && assistantRun.pending?.call_id" class="assistant-answer" @submit.prevent="answerAssistant">
                    <label for="assistant-answer">{{ assistantRun.pending.question || '请补充小菱需要的信息' }}</label>
                    <textarea id="assistant-answer" v-model="assistantAnswer" maxlength="4000" rows="2" placeholder="输入补充信息" />
                    <button type="submit" :disabled="assistantActionLoading || !assistantAnswer.trim()">{{ assistantActionLoading ? '提交中…' : '继续' }}</button>
                  </form>
                  <div class="assistant-run-actions">
                    <button v-if="assistantRun.recoverable" type="button" :disabled="assistantActionLoading" @click="retryAssistant">恢复运行</button>
                    <button v-if="assistantRun.status === 'waiting_approval' && canManageCompany" type="button"
                      @click="switchSection('assistant-approvals')">查看审批</button>
                    <button v-if="assistantRunCanCancel" type="button" class="assistant-cancel" :disabled="assistantActionLoading" @click="cancelAssistant">停止本次运行</button>
                  </div>
                  <p v-if="assistantRun.error" class="assistant-run-error">
                    {{ assistantRunErrorLabel(assistantRun.error, assistantRun.status) }}
                  </p>
                </section>
                <form class="message-composer composer" @submit.prevent="send">
                  <label class="sr-only" for="message-content">消息内容</label>
                  <textarea id="message-content" v-model="draft" maxlength="10000" :disabled="!roomVerified" placeholder="输入消息，Enter 发送，Shift + Enter 换行" rows="3" @keydown="handleComposerKeydown" />
                  <p v-if="billingContextState !== 'idle'" class="assistant-billing-context" role="status">
                    {{ billingContextLabel }}
                    <button v-if="billingContextState === 'error'" type="button" data-action="retry-billing-context" @click="loadBillingContext">重试</button>
                  </p>
                  <div class="composer-footer"><span>{{ draft.length }}/10000 · @小菱</span><button type="submit" :disabled="!roomVerified || sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button></div>
                </form>
                <p v-if="!assistantAvailable" class="assistant-status-note" role="status">小菱暂不可用，请检查订阅与积分，或联系管理员。</p>
              </template>
              <div v-else class="chat-welcome"><span class="welcome-avatar">企</span><h2>选择一个会话</h2><p>选择公司群、小组群，或开启同事私聊。</p><button type="button" @click="openCompany">打开公司群</button></div>
              <FloatingChatResizeHandles v-if="chatFloating" @resize-start="beginChatResize" @resize-keydown="resizeChatByKeyboard" />
            </section>
          </Teleport>
        </div>
      </div>
      <EnterpriseManagement v-if="activeSection === 'management' && companyId && currentUserId && canManageGroups"
        :key="`${companyId}:${currentUserId}`" :enterprise-id="companyId" :current-user-id="currentUserId"
        :role="currentCompanyRole" @access-lost="clearEnterpriseAccess" />
      <EnterpriseUsage v-if="activeSection === 'usage' && companyId && currentUserId && canManageGroups"
        :key="`${companyId}:${currentUserId}`" :enterprise-id="companyId" :role="currentCompanyRole"
        :groups="groups" :directory="directory" :directory-has-more="directoryHasMore"
        @load-more-members="loadMoreMembers" @access-lost="clearEnterpriseAccess" />
      <EnterpriseAssistantApprovals v-if="activeSection === 'assistant-approvals' && companyId && canManageCompany"
        :key="`${companyId}:${currentUserId}:assistant-approvals`" :enterprise-id="companyId"
        @open-conversation="openAssistantApprovalConversation" />
      <EnterpriseRemoteTests v-if="activeSection === 'remote-tests' && companyId && currentUserId"
        :key="`${companyId}:${currentUserId}:remote-tests`" :enterprise-id="companyId" :current-user-id="currentUserId"
        :role="currentCompanyRole" :groups="groups" @access-lost="clearEnterpriseAccess" />
      <EnterpriseKnowledge v-if="activeSection === 'knowledge' && companyId && currentUserId"
        :key="`${companyId}:${currentUserId}`" :enterprise-id="companyId" :current-user-id="currentUserId"
        :can-manage="canManageCompany" @access-lost="clearEnterpriseAccess" />
      <EnterpriseAssets v-if="activeSection === 'assets' && companyId && currentUserId" ref="assetsRef"
        :key="`${companyId}:${currentUserId}`" :enterprise-id="companyId" :current-user-id="currentUserId"
        :role="currentCompanyRole" @access-lost="clearEnterpriseAccess" />
      <EnterpriseCommunity v-if="(activeSection === 'forum' || activeSection === 'announcement') && companyId && currentUserId" :key="`${companyId}:${activeSection}`"
        :enterprise-id="companyId" :kind="activeSection" :can-manage="canManageCompany"
        :current-user-id="currentUserId" :directory="directory" @access-lost="clearEnterpriseAccess" />
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  acceptEnterpriseInvitation, answerEnterpriseAssistantRun, cancelEnterpriseAssistantRun,
  decideEnterpriseAssistantClarification, getEnterpriseAssistantBillingContext,
  getEnterpriseAssistantClarification, getEnterpriseAssistantRun, getEnterpriseMessages,
  listEnterpriseAssistantRuns, listEnterpriseConversations, listEnterpriseDirectory,
  listEnterpriseGroups, listMyEnterprises, openEnterpriseConversation, requestEnterpriseAssistant,
  resumeEnterpriseAssistantClarification, retryEnterpriseAssistantRun, sendEnterpriseMessage,
  type Enterprise, type EnterpriseConversation, type EnterpriseDirectoryMember, type EnterpriseGroup,
  type EnterpriseAssistantBillingContext, type EnterpriseAssistantClarification,
  type EnterpriseAssistantClarificationCandidate, type EnterpriseAssistantClarificationChoice,
  type EnterpriseAssistantRun, type EnterpriseMessage,
} from '@/api/enterprise'
import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'
import { getToken } from '@/utils/token'
import EnterpriseCommunity from './EnterpriseCommunity.vue'
import EnterpriseManagement from './EnterpriseManagement.vue'
import EnterpriseUsage from './EnterpriseUsage.vue'
import EnterpriseKnowledge from './EnterpriseKnowledge.vue'
import EnterpriseAssets from './EnterpriseAssets.vue'
import EnterpriseAssistantApprovals from './EnterpriseAssistantApprovals.vue'
import EnterpriseRemoteTests from './EnterpriseRemoteTests.vue'
import FloatingChatResizeHandles from '@/components/ai/FloatingChatResizeHandles.vue'
import { useFloatingChatPosition } from '@/composables/useFloatingChatPosition'
import { useChatAutoScroll } from '@/composables/useChatAutoScroll'
import type { FloatingChatResizeDirection } from '@/composables/useFloatingChatPosition'
import EnterpriseAgentTeamProgress from './EnterpriseAgentTeamProgress.vue'
import EnterpriseProactiveSettings from './EnterpriseProactiveSettings.vue'

const user = useUserStore()
const workspace = useWorkspaceStore()
const currentUserId = computed(() => user.profile?.id)
type Section = 'chat' | 'forum' | 'announcement' | 'knowledge' | 'assets' | 'management' | 'usage' | 'assistant-approvals' | 'remote-tests'
const activeSection = ref<Section>('chat')
const proactiveSettingsOpen = ref(false)
const assetsRef = ref<{ mayLeave: () => boolean } | null>(null)
const companies = ref<Enterprise[]>([])
const companyId = ref<number | null>(null)
const groups = ref<EnterpriseGroup[]>([])
const directory = ref<EnterpriseDirectoryMember[]>([])
const rooms = ref<EnterpriseConversation[]>([])
const roomId = ref<number | null>(null)
const messages = ref<EnterpriseMessage[]>([])
const assistantAvailable = ref(false)
const billingContext = ref<EnterpriseAssistantBillingContext | null>(null)
const billingContextState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const assistantRetry = ref<{ enterpriseId: number; conversationId: number; sourceMessageId: number;
  accountId: number; token: string | null } | null>(null)
const assistantRun = ref<EnterpriseAssistantRun | null>(null)
const assistantRunConversationId = ref<number | null>(null)
const assistantClarification = ref<EnterpriseAssistantClarification | null>(null)
const assistantClarificationLoading = ref(false)
const assistantAnswer = ref('')
const assistantActionLoading = ref(false)
const hasMore = ref(false)
const loadingCompanies = ref(false)
const loadingMessages = ref(false)
const joining = ref(false)
const sendingRooms = ref<Set<string>>(new Set())
const invitation = ref('')
const showInvitation = ref(false)
const directUserId = ref(0)
const directoryHasMore = ref(false)
const draft = ref('')
const roomVerified = ref(false)
const pageError = ref('')
const chatError = ref('')
const roomFilter = ref('')
const chatFloating = ref(false)
const teamProgressOpen = ref(false)
const chatWindowReturnFocus = ref<HTMLElement | null>(null)
const messagesContainer = ref<HTMLElement | null>(null)
const enterprisePageRef = ref<HTMLElement | null>(null)
const enterpriseHeadingRef = ref<HTMLElement | null>(null)
const {
  panelRef: chatPanelRef, style: chatPanelStyle, dragging: chatDragging, resizing: chatResizing,
  restoreOrAnchor: restoreChatWindow, beginDrag: beginChatDrag, beginResize: beginWindowResize,
  resizeByKeyboard: resizeChatByKeyboard, moveByKeyboard: moveChatByKeyboard,
  moveDrag: moveChatDrag, endDrag: endChatDrag,
} = useFloatingChatPosition('enterprise', 760)
const { pinned: messagesPinned, scrollToBottom, followIfPinned } = useChatAutoScroll(messagesContainer)
const selectedRoom = computed(() => rooms.value.find(item => item.id === roomId.value))
const billingContextLabel = computed(() => {
  if (billingContextState.value === 'loading') return '正在读取计费归属…'
  if (billingContextState.value === 'error') return '无法读取计费归属。'
  const context = billingContext.value
  if (!context) return ''
  const label = context.status === 'group' ? `预计计费：${context.group_name}`
    : context.status === 'unassigned' ? '预计计费：未分组' : '请联系管理员设置默认小组'
  return context.enabled ? label : `计费归属尚未启用 · ${label}`
})
const activeAssistantRoom = computed(() => Boolean(assistantRun.value && assistantRunConversationId.value === roomId.value))
const assistantRunCanCancel = computed(() => Boolean(assistantRun.value &&
  ['queued', 'running', 'waiting_input', 'waiting_approval', 'awaiting_clarification'].includes(assistantRun.value.status)))
const otherMembers = computed(() => directory.value.filter(item => item.user_id !== currentUserId.value))
const filteredRooms = computed(() => {
  const query = roomFilter.value.toLocaleLowerCase()
  return rooms.value.filter(room => !query || roomTitle(room).toLocaleLowerCase().includes(query))
})
const currentCompanyName = computed(() => companies.value.find(item => item.id === companyId.value)?.name || '企业空间')
const currentCompanyMeta = computed(() => {
  const company = companies.value.find(item => item.id === companyId.value)
  if (!company) return '企业微信'
  const state = ({ active: '订阅有效', grace: '续费宽限期', expired: '订阅已过期', none: '订阅状态待确认' })[
    company.subscription_status || 'none'
  ]
  const seats = company.seat_limit == null || company.active_member_count == null
    ? '' : `席位 ${company.active_member_count}/${company.seat_limit}${company.over_capacity ? '（超额）' : ''}`
  return [state, seats].filter(Boolean).join(' · ')
})
const companyInitial = computed(() => currentCompanyName.value.trim().slice(0, 1) || '企')
const currentUserName = computed(() => user.profile?.username || user.profile?.nickname || '我')
const currentCompanyRole = computed(() => companies.value.find(item => item.id === companyId.value)?.role || 'member')
const canManageCompany = computed(() => ['owner', 'admin'].includes(currentCompanyRole.value))
const canManageGroups = computed(() => canManageCompany.value || groups.value.some(group => group.my_role === 'leader'))
const canManageSelectedGroup = computed(() => Boolean(selectedRoom.value?.kind === 'group'
  && groups.value.some(group => group.id === selectedRoom.value?.group_id && group.my_role === 'leader')))
const chatGroups = computed(() => groups.value.filter(group => Boolean(group.my_role)))
const sending = computed(() => currentUserId.value != null && companyId.value !== null && roomId.value !== null
  && sendingRooms.value.has(`${currentUserId.value}:${companyId.value}:${roomId.value}`))
let authGeneration = 0
let clarificationGeneration = 0
let resourcesStale = false
let workspaceGeneration = 0
let companyGeneration = 0
let roomGeneration = 0
let openGeneration = 0
let fetchedCursor = 0
let oldestCursor = 0
let messagesInitialized = false
let loadingRoomGeneration: number | null = null
let poll: ReturnType<typeof setInterval> | null = null
let lastWorkspaceRefreshAt = 0
let lastResumeRefreshAt = Number.NEGATIVE_INFINITY
let billingContextSequence = 0
let billingContextToken = getToken()
let billingScopeInvalid = false
let billingContextRequest: AbortController | null = null
const WORKSPACE_REFRESH_INTERVAL = 30_000
const pendingIds = new Map<string, string>()
// Drafts remain in memory only, scoped to the authenticated account, enterprise and conversation.
const roomDrafts = new Map<string, { content: string; groupId: number | null }>()
let fallbackMessageSequence = 0

function roomDraftKey(accountId: number, enterpriseId: number, conversationId: number) {
  return `${accountId}:${enterpriseId}:${conversationId}`
}
function rememberRoomDraft() {
  const accountId = currentUserId.value, enterpriseId = companyId.value, conversationId = roomId.value
  if (!accountId || !enterpriseId || !conversationId || !roomVerified.value) return
  const key = roomDraftKey(accountId, enterpriseId, conversationId)
  if (draft.value) roomDrafts.set(key, { content: draft.value, groupId: selectedRoom.value?.group_id ?? null })
  else roomDrafts.delete(key)
}
function clearEnterpriseDrafts(accountId: number, enterpriseId: number) {
  const prefix = `${accountId}:${enterpriseId}:`
  for (const key of roomDrafts.keys()) if (key.startsWith(prefix)) roomDrafts.delete(key)
}
function pruneGroupDrafts(accountId: number, enterpriseId: number, memberGroupIds: Set<number>) {
  const prefix = `${accountId}:${enterpriseId}:`
  for (const [key, value] of roomDrafts) {
    if (key.startsWith(prefix) && value.groupId !== null && !memberGroupIds.has(value.groupId)) roomDrafts.delete(key)
  }
}
function pruneFormerEnterprises(accountId: number, enterpriseIds: Set<number>) {
  for (const key of roomDrafts.keys()) {
    const [owner, enterprise] = key.split(':').map(Number)
    if (owner === accountId && !enterpriseIds.has(enterprise)) roomDrafts.delete(key)
  }
}

function createClientMessageId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  const random = globalThis.crypto?.getRandomValues
    ? Array.from(globalThis.crypto.getRandomValues(new Uint32Array(2)), value => value.toString(36)).join('')
    : Math.random().toString(36).slice(2)
  fallbackMessageSequence += 1
  return `msg_${Date.now().toString(36)}_${fallbackMessageSequence.toString(36)}_${random}`
}

async function pendingMessageKey(userId: number, enterpriseId: number, conversationId: number, content: string) {
  const encoded = new TextEncoder().encode(content)
  let hash: string
  if (globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest('SHA-256', encoded)
    hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  } else {
    // Idempotency only needs a stable local key. Avoid making message delivery
    // depend on WebCrypto availability in older or non-secure browser contexts.
    let value = 0xcbf29ce484222325n
    for (const byte of encoded) value = ((value ^ BigInt(byte)) * 0x100000001b3n) & 0xffffffffffffffffn
    hash = value.toString(16).padStart(16, '0')
  }
  const key = `prism:pending-message:${userId}:${enterpriseId}:${conversationId}:${hash}`
  let id = pendingIds.get(key)
  try { id ||= sessionStorage.getItem(key) || undefined } catch { /* 浏览器禁用存储时仍保留本页内幂等键。 */ }
  if (!id) {
    id = createClientMessageId()
    try { sessionStorage.setItem(key, id) } catch { /* 仅内存可用时不能跨刷新恢复。 */ }
  }
  pendingIds.set(key, id)
  return { key, id }
}

function clearPendingMessage(key: string) {
  pendingIds.delete(key)
  try { sessionStorage.removeItem(key) } catch { /* 已确认成功，不影响消息结果。 */ }
}
function assistantRecoveryKey(accountId: number, enterpriseId: number, conversationId: number) {
  return `prism:assistant-recovery:${accountId}:${enterpriseId}:${conversationId}`
}
function saveAssistantRecovery(accountId: number, enterpriseId: number,
                               conversationId: number, sourceMessageId: number) {
  try {
    sessionStorage.setItem(assistantRecoveryKey(accountId, enterpriseId, conversationId), String(sourceMessageId))
  } catch { /* 当前页面仍可重试；禁用会话存储时不持久化。 */ }
}
function loadAssistantRecovery(accountId: number, enterpriseId: number, conversationId: number) {
  const key = assistantRecoveryKey(accountId, enterpriseId, conversationId)
  let raw: string | null
  try { raw = sessionStorage.getItem(key) } catch { return null }
  if (raw === null) return null
  const sourceMessageId = Number(raw)
  if (Number.isSafeInteger(sourceMessageId) && sourceMessageId > 0) return sourceMessageId
  try { sessionStorage.removeItem(key) } catch { /* 忽略损坏的本地记录。 */ }
  return null
}
function clearAssistantRecovery(accountId: number, enterpriseId: number, conversationId: number) {
  try { sessionStorage.removeItem(assistantRecoveryKey(accountId, enterpriseId, conversationId)) }
  catch { /* 清理失败不影响服务端授权与账本。 */ }
}
function clearEnterpriseAccess() {
  if (currentUserId.value && companyId.value) clearEnterpriseDrafts(currentUserId.value, companyId.value)
  if (currentUserId.value && companyId.value && roomId.value) {
    clearAssistantRecovery(currentUserId.value, companyId.value, roomId.value)
  }
  assistantRetry.value = null
  assistantRun.value = null; assistantRunConversationId.value = null; assistantAnswer.value = ''
  clearAssistantClarification()
  closeChatWindow(false)
  teamProgressOpen.value = false
  resourcesStale = true
  ++companyGeneration; ++roomGeneration; ++openGeneration
  roomId.value = null; messages.value = []; rooms.value = []; groups.value = []; directory.value = []
  hasMore.value = false; directoryHasMore.value = false; fetchedCursor = 0; draft.value = ''; roomVerified.value = false
  activeSection.value = 'chat'
  chatError.value = '企业内容权限已变化，请刷新企业列表。'
}

function memberName(id: number | null) {
  if (id == null) return '企业成员'
  return directory.value.find(item => item.user_id === id)?.display_name || `成员 ${id}`
}
function roomTitle(room: EnterpriseConversation) {
  if (room.kind === 'company') return '公司群'
  if (room.kind === 'group') return groups.value.find(item => item.id === room.group_id)?.name || '小组群'
  const other = room.participant_low_id === currentUserId.value ? room.participant_high_id : room.participant_low_id
  return other ? `与 ${memberName(other)} 的私聊` : '私聊'
}
function roomKindLabel(room: EnterpriseConversation) {
  return room.kind === 'company' ? '公司全员群' : room.kind === 'group' ? '企业小组群' : '企业成员私聊'
}
function roomKindShort(room: EnterpriseConversation) {
  return room.kind === 'company' ? '公司' : room.kind === 'group' ? '小组' : '私聊'
}
function roomAvatar(room: EnterpriseConversation) {
  return room.kind === 'company' ? '企' : room.kind === 'group'
    ? (groups.value.find(group => group.id === room.group_id)?.name.slice(0, 1) || '组')
    : roomTitle(room).replace(/^与\s*/, '').slice(0, 1)
}
function formatMessageTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
}
function assistantStatusLabel(status: EnterpriseAssistantRun['status']) {
  const labels: Record<EnterpriseAssistantRun['status'], string> = {
    queued: '小菱已收到，等待处理', running: '小菱正在处理', waiting_approval: '等待管理员审批',
    waiting_input: '等待你补充信息', awaiting_clarification: '等待权限确认',
    completed: '小菱已完成', failed: '运行失败',
    incomplete: '运行未完成', max_rounds_exceeded: '已达到运行轮数上限', cancelled: '运行已停止',
    retryable: '运行中断，可恢复', suppressed: '结果因权限变化已隐藏',
    awaiting_reconciliation: '模型请求状态待核对',
  }
  return labels[status]
}
function clarificationActionLabel(action: string) {
  const labels: Record<string, string> = {
    send_enterprise_assistant_response: '发送本次小菱回复',
    search_enterprise_web: '搜索公开网页',
    fetch_enterprise_web_page: '读取公开网页',
    create_registered_enterprise_agent_team: '创建企业 Agent 团队',
    get_enterprise_agent_team: '查看 Agent 团队',
    retry_enterprise_agent_team_task: '重试团队任务',
    run_approved_enterprise_asset_team: '运行已发布 Agent/Skill 组合',
    invoke_published_enterprise_asset: '运行已发布 Agent/Skill',
    search_published_enterprise_assets: '搜索已发布 Agent/Skill',
    search_enterprise_knowledge: '搜索企业知识',
    create_private_agent_skill_draft: '创建私有 Agent/Skill 草稿',
    propose_enterprise_announcement: '提交企业公告提案',
  }
  return labels[action] || '待确认的操作'
}
function clarificationDecisionLabel(decision: EnterpriseAssistantClarificationCandidate['decision']) {
  const labels = { pending: '待确认', allow: '允许本次', deny: '阻止本次', unrelated: '与本次无关' }
  return labels[decision]
}
function assistantRunErrorLabel(error: string | null | undefined, status: EnterpriseAssistantRun['status']) {
  if (error === 'billing_attribution_unavailable') {
    return '小菱暂不可用，请稍后再试。'
  }
  if (error === 'provider_attempt_released_not_sent') {
    if (status === 'cancelled') {
      return '平台已通过双人复核确认模型请求未发送；本次运行已停止，不会自动续跑。'
    }
    return '平台已通过双人复核确认上次请求未发送。检查点可安全恢复；点击“恢复运行”后会显式重新发起模型请求。'
  }
  if (error === 'provider_result_missing_after_settlement') {
    return '模型账单已核对，但答复检查点没有保存。为避免重复执行，系统不会重发；请联系平台管理员处理。'
  }
  if (error === 'provider_attempt_pending_reconciliation') {
    if (status === 'cancelled') {
      return '本次运行已停止；已发出模型请求的状态仍待核对，后续不会自动续跑。请联系平台管理员核对。'
    }
    return '模型请求送达、费用结算或运行结果尚未确认。为避免重复调用，运行已暂停；请联系平台管理员核对。'
  }
  return error || ''
}
function assistantBillingUnavailable(error: unknown): string | null {
  const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
  if (code === 40360) return '小菱暂不可用，请稍后再试。'
  if (code === 40361) return '请联系管理员设置默认小组。'
  return null
}
function acceptAssistantRun(run: EnterpriseAssistantRun, enterpriseId: number, conversationId: number,
                            accountId: number, token: string | null) {
  if (run.message && !messages.value.some(item => item.id === run.message!.id)) {
    messages.value.push(run.message)
    messages.value.sort((a, b) => a.id - b.id)
  } else if (run.message) {
    messages.value = messages.value.map(item => item.id === run.message!.id ? run.message! : item)
  }
  assistantRun.value = run
  assistantRunConversationId.value = conversationId
  assistantAnswer.value = ''
  if (run.status === 'awaiting_clarification') {
    void refreshAssistantClarification(run, enterpriseId, conversationId, accountId, token)
  } else {
    clearAssistantClarification()
  }
  if (run.recoverable && run.source_message_id) {
    assistantRetry.value = { enterpriseId, conversationId, sourceMessageId: run.source_message_id, accountId, token }
    saveAssistantRecovery(accountId, enterpriseId, conversationId, run.source_message_id)
  } else {
    assistantRetry.value = null
    clearAssistantRecovery(accountId, enterpriseId, conversationId)
  }
}
function clearAssistantClarification() {
  ++clarificationGeneration
  assistantClarification.value = null
  assistantClarificationLoading.value = false
}
async function refreshAssistantClarification(run: EnterpriseAssistantRun, enterpriseId: number,
                                             conversationId: number, accountId: number, token: string | null) {
  const requestId = run.pending?.approval_id ?? run.pending?.preview?.request_id
  if (!requestId) {
    clearAssistantClarification()
    return
  }
  const auth = authGeneration, generation = roomGeneration, requestGeneration = ++clarificationGeneration
  assistantClarificationLoading.value = true
  try {
    const result = await getEnterpriseAssistantClarification(enterpriseId, conversationId, run.run_id, token)
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)
      || assistantRun.value?.run_id !== run.run_id || clarificationGeneration !== requestGeneration
      || result.request_id !== requestId) return
    assistantClarification.value = result
  } catch (error) {
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)
      || clarificationGeneration !== requestGeneration) return
    assistantClarification.value = null
    const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
  } finally {
    if (clarificationGeneration === requestGeneration) assistantClarificationLoading.value = false
  }
}
function assistantRequestIsCurrent(auth: number, accountId: number, token: string | null,
                                   generation: number, enterpriseId: number, conversationId: number) {
  return auth === authGeneration && currentUserId.value === accountId && token === getToken()
    && generation === roomGeneration && conversationId === roomId.value && enterpriseId === companyId.value
}
async function restoreAssistantRun(enterpriseId: number, conversationId: number,
                                   accountId: number, token: string | null, generation: number) {
  const auth = authGeneration
  try {
    const result = await listEnterpriseAssistantRuns(enterpriseId, conversationId)
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)) return
    const active = result.items[0]
    if (active) {
      acceptAssistantRun(active, enterpriseId, conversationId, accountId, token)
      chatError.value = ''
      return
    }
    const sourceMessageId = loadAssistantRecovery(accountId, enterpriseId, conversationId)
    if (sourceMessageId) {
      assistantRetry.value = { enterpriseId, conversationId, sourceMessageId, accountId, token }
      chatError.value = '发现一条未确认的小菱请求；可安全重新关联原请求，不会再次发送聊天消息。'
    } else {
      assistantRetry.value = null
      assistantRun.value = null
      assistantRunConversationId.value = null
      clearAssistantClarification()
    }
  } catch (error) {
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)) return
    const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
  }
}
async function pollAssistantRun() {
  const run = assistantRun.value
  const enterpriseId = companyId.value, conversationId = roomId.value, accountId = currentUserId.value
  const token = getToken(), generation = roomGeneration, auth = authGeneration
  if (!run || !enterpriseId || !conversationId || !accountId || assistantRunConversationId.value !== conversationId
    || ['completed', 'cancelled', 'suppressed'].includes(run.status)) return
  try {
    const result = await getEnterpriseAssistantRun(enterpriseId, conversationId, run.run_id, token)
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)
      || assistantRun.value?.run_id !== run.run_id) return
    acceptAssistantRun(result, enterpriseId, conversationId, accountId, token)
  } catch (error) {
    if (!assistantRequestIsCurrent(auth, accountId, token, generation, enterpriseId, conversationId)) return
    const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
  }
}
function beginChatWindowMove(event: PointerEvent) {
  if (!chatFloating.value) return
  beginChatDrag(event)
}
function moveChatWindowByKeyboard(event: KeyboardEvent) {
  if (!chatFloating.value) return
  moveChatByKeyboard(event)
}
function moveChatWindow(event: PointerEvent) { moveChatDrag(event) }
function endChatWindowMove(event: PointerEvent) { endChatDrag(event) }
function beginChatResize(event: PointerEvent, direction: FloatingChatResizeDirection) {
  beginWindowResize(event, direction)
}
function restoreChatWindowFocus(target: HTMLElement | null) {
  if (target?.isConnected) {
    target.focus({ preventScroll: true })
    return
  }
  const activeTab = document.querySelector<HTMLElement>('.workspace-tabs [aria-pressed="true"]')
  const fallback = activeTab ?? document.querySelector<HTMLElement>('#company-select')
  fallback?.focus({ preventScroll: true })
}
async function toggleChatWindow(event: MouseEvent) {
  if (!chatFloating.value) {
    const trigger = event.currentTarget
    chatWindowReturnFocus.value = trigger instanceof HTMLElement
      ? trigger
      : document.activeElement instanceof HTMLElement ? document.activeElement : null
    chatFloating.value = true
    await nextTick()
    restoreChatWindow()
    chatPanelRef.value?.focus({ preventScroll: true })
  } else {
    chatFloating.value = false
    await nextTick()
    restoreChatWindowFocus(chatWindowReturnFocus.value)
    chatWindowReturnFocus.value = null
  }
}
function closeChatWindow(restoreFocus = true) {
  const wasFloating = chatFloating.value
  const returnFocus = chatWindowReturnFocus.value
  chatWindowReturnFocus.value = null
  if (!wasFloating) return
  chatFloating.value = false
  void nextTick(() => {
    if (restoreFocus) restoreChatWindowFocus(returnFocus)
    else document.querySelector<HTMLElement>('#company-select')?.focus({ preventScroll: true })
  })
}
function handleFloatingWindowKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !chatFloating.value) return
  if (event.defaultPrevented || event.isComposing) return
  const target = event.target instanceof Element ? event.target : null
  const outsideChat = target && !chatPanelRef.value?.contains(target)
  if (outsideChat && target.closest(
    'input, textarea, select, [contenteditable="true"], [role="combobox"], [role="listbox"], [role="menu"], [role="dialog"], [aria-modal="true"], [aria-expanded="true"]',
  )) return
  event.preventDefault()
  event.stopPropagation()
  closeChatWindow()
}
function backToConversations() {
  rememberRoomDraft()
  closeChatWindow()
  teamProgressOpen.value = false
  roomId.value = null
  messages.value = []
  draft.value = ''; roomVerified.value = false
}
function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}
function switchSection(section: Section) {
  if (activeSection.value === 'assets' && section !== 'assets' && assetsRef.value && !assetsRef.value.mayLeave()) return
  if (section !== 'chat' && chatFloating.value && (window.innerWidth <= 760 || window.innerHeight <= 520)) {
    closeChatWindow()
  }
  if (activeSection.value === 'management' && section !== 'management' && companyId.value) {
    void refreshWorkspaceResources(companyId.value, companyGeneration)
  }
  activeSection.value = section
}
async function refreshWorkspaceResources(id: number, current: number, includeConversations = true) {
  const workspace = ++workspaceGeneration
  try {
    const [currentCompanies, groupData, people, conversations] = await Promise.all([
      listMyEnterprises(), listEnterpriseGroups(id), listEnterpriseDirectory(id),
      includeConversations ? listEnterpriseConversations(id) : Promise.resolve(null),
    ])
    if (current !== companyGeneration || workspace !== workspaceGeneration || companyId.value !== id) return
    companies.value = currentCompanies
    if (currentUserId.value) pruneFormerEnterprises(currentUserId.value, new Set(currentCompanies.map(company => company.id)))
    if (!currentCompanies.some(company => company.id === id)) {
      clearEnterpriseAccess()
      companyId.value = null
      if (currentCompanies.length) await chooseCompany(currentCompanies[0].id)
      return
    }
    const previouslyAccessibleGroups = new Set(groups.value.filter(group => group.my_role).map(group => group.id))
    if ([...previouslyAccessibleGroups].some(groupId => !groupData.some(group => group.id === groupId && group.my_role))) {
      ++openGeneration
    }
    const room = selectedRoom.value
    const permittedGroupIds = new Set(groupData.filter(group => Boolean(group.my_role)).map(group => group.id))
    groups.value = groupData; directory.value = people; directoryHasMore.value = people.length === 100
    if (currentUserId.value && companyId.value) pruneGroupDrafts(currentUserId.value, companyId.value, permittedGroupIds)
    const refreshedRooms = conversations
      ? conversations.items.filter(item => item.kind !== 'group'
        || (item.group_id != null && permittedGroupIds.has(item.group_id)))
      : rooms.value.filter(item => item.kind !== 'group'
        || (item.group_id != null && permittedGroupIds.has(item.group_id)))
    if (room && !refreshedRooms.some(item => item.id === room.id)) refreshedRooms.push(room)
    rooms.value = refreshedRooms
    if (conversations) assistantAvailable.value = conversations.assistant_available
    if (room?.kind === 'group' && !groupData.some(group => group.id === room.group_id && group.my_role)) {
      closeChatWindow(false)
      if (currentUserId.value && companyId.value) {
        clearAssistantRecovery(currentUserId.value, companyId.value, room.id)
      }
      if (assistantRetry.value?.conversationId === room.id) assistantRetry.value = null
      if (assistantRunConversationId.value === room.id) {
        assistantRun.value = null
        assistantRunConversationId.value = null
        clearAssistantClarification()
      }
      assistantAnswer.value = ''
      assistantActionLoading.value = false
      ++openGeneration; ++roomGeneration; roomId.value = null; messages.value = []; draft.value = ''; roomVerified.value = false
      hasMore.value = false; fetchedCursor = 0; oldestCursor = 0; messagesInitialized = false
      chatError.value = '小组会话权限已变化，已关闭当前内容。'
    }
  } catch (cause) {
    if (current === companyGeneration && workspace === workspaceGeneration) {
      const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
      if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
        clearEnterpriseAccess()
        await loadCompanies()
      } else chatError.value = '企业信息刷新失败，请重试。'
    }
  }
}
async function loadCompanies() {
  const accountId = currentUserId.value
  const auth = authGeneration
  const isCurrent = () => auth === authGeneration && accountId === currentUserId.value
  loadingCompanies.value = true
  pageError.value = ''
  try {
    const result = await listMyEnterprises()
    if (!isCurrent()) return
    companies.value = result
    if (accountId) pruneFormerEnterprises(accountId, new Set(result.map(item => item.id)))
    if (companyId.value && result.some(item => item.id === companyId.value)) {
      if (resourcesStale) await chooseCompany(companyId.value)
      else await refreshWorkspaceResources(companyId.value, companyGeneration)
      return
    }
    const savedEnterpriseId = workspace.activeSpace === 'enterprise' ? workspace.activeEnterpriseId : null
    if (savedEnterpriseId && result.some(item => item.id === savedEnterpriseId)) {
      await chooseCompany(savedEnterpriseId)
      return
    }
    else if (result.length) await chooseCompany(result[0].id)
    else {
      workspace.activateEnterprise(null)
      ++companyGeneration; ++roomGeneration; ++openGeneration
      companyId.value = null; roomId.value = null; messages.value = []; rooms.value = []
      groups.value = []; directory.value = []; draft.value = ''
    }
  } catch { if (isCurrent()) pageError.value = '企业列表加载失败，请重试。' }
  finally { if (isCurrent()) loadingCompanies.value = false }
}
async function joinEnterprise() {
  const accountId = currentUserId.value
  const auth = authGeneration
  const isCurrent = () => auth === authGeneration && accountId === currentUserId.value
  joining.value = true
  pageError.value = ''
  try {
    const result = await acceptEnterpriseInvitation(invitation.value)
    if (!isCurrent()) return
    invitation.value = ''; showInvitation.value = false
    await loadCompanies()
    if (!isCurrent()) return
    if (companies.value.some(item => item.id === result.enterprise_id)) await chooseCompany(result.enterprise_id)
  }
  catch { if (isCurrent()) pageError.value = '加入失败，请检查邀请是否有效或联系企业管理员。' }
  finally { if (isCurrent()) joining.value = false }
}
async function chooseCompany(id: number) {
  rememberRoomDraft()
  if (companyId.value !== id) proactiveSettingsOpen.value = false
  workspace.activateEnterprise(id)
  closeChatWindow(false)
  teamProgressOpen.value = false
  const current = ++companyGeneration
  ++openGeneration
  ++roomGeneration
  if (companyId.value !== id) activeSection.value = 'chat'
  companyId.value = id
  roomId.value = null
  groups.value = []; directory.value = []; rooms.value = []; messages.value = []
  assistantAvailable.value = false
  assistantRun.value = null; assistantRunConversationId.value = null; assistantAnswer.value = ''
  clearAssistantClarification()
  hasMore.value = false; directoryHasMore.value = false; directUserId.value = 0
  fetchedCursor = 0; loadingMessages.value = false; draft.value = ''; roomVerified.value = false; chatError.value = ''
  try {
    const [groupData, people, conversations] = await Promise.all([
      listEnterpriseGroups(id), listEnterpriseDirectory(id), listEnterpriseConversations(id),
    ])
    if (current !== companyGeneration) return
    resourcesStale = false
    if (currentUserId.value) pruneGroupDrafts(currentUserId.value, id,
      new Set(groupData.filter(group => Boolean(group.my_role)).map(group => group.id)))
    groups.value = groupData; directory.value = people; directoryHasMore.value = people.length === 100
    rooms.value = conversations.items
    assistantAvailable.value = conversations.assistant_available
  } catch { if (current === companyGeneration) chatError.value = '企业会话加载失败，请刷新后重试。' }
}
async function loadMoreMembers() {
  const id = companyId.value, current = companyGeneration
  if (!id) return
  try {
    const people = await listEnterpriseDirectory(id, directory.value.length)
    if (current !== companyGeneration) return
    const seen = new Set(directory.value.map(item => item.user_id))
    directory.value.push(...people.filter(item => !seen.has(item.user_id)))
    directoryHasMore.value = people.length === 100
  } catch { if (current === companyGeneration) chatError.value = '成员列表加载失败，请重试。' }
}
function handleEmptyRoomsAction() {
  if (roomFilter.value) roomFilter.value = ''
  else void openCompany()
}
async function refreshVisibleState(forceWorkspace = false) {
  if (document.visibilityState !== 'visible') return
  if (!billingScopeInvalid && billingContextToken !== getToken()) invalidateBillingScope()
  const now = Date.now()
  const refreshWorkspace = forceWorkspace || now - lastWorkspaceRefreshAt >= WORKSPACE_REFRESH_INTERVAL
  // 计费预览有自己的服务端会话鉴权；组织列表较慢时也要及时刷新旧归属。
  const billingRefresh = refreshWorkspace ? loadBillingContext() : Promise.resolve()
  if (refreshWorkspace) {
    lastWorkspaceRefreshAt = now
    const id = companyId.value
    if (id) await refreshWorkspaceResources(id, companyGeneration, forceWorkspace)
    else await loadCompanies()
  }
  await Promise.all([refreshMessages(), pollAssistantRun(), billingRefresh])
}
function refreshOnResume() {
  if (document.visibilityState !== 'visible') return
  const now = Date.now()
  if (now - lastResumeRefreshAt < 1000) return
  lastResumeRefreshAt = now
  void refreshVisibleState(true)
}
function handleVisibilityChange() {
  if (document.visibilityState === 'visible') refreshOnResume()
}
async function openRoom(input: { kind: 'company' | 'group' | 'direct'; group_id?: number; other_user_id?: number }) {
  const id = companyId.value
  const current = companyGeneration
  if (!id) return
  const opening = ++openGeneration
  chatError.value = ''
  try {
    const room = await openEnterpriseConversation(id, input)
    if (current !== companyGeneration || opening !== openGeneration) return
    if (input.kind === 'group' && !groups.value.some(group => group.id === input.group_id && group.my_role)) {
      chatError.value = '小组会话权限已变化，无法打开当前内容。'
      return
    }
    if (!rooms.value.some(item => item.id === room.id)) rooms.value.push(room)
    await selectRoom(room.id)
  } catch {
    if (current === companyGeneration && opening === openGeneration) chatError.value = '无法打开会话，请检查你是否仍在该企业或小组。'
  }
}
const openCompany = () => openRoom({ kind: 'company' })
const openGroup = (group: EnterpriseGroup) => openRoom({ kind: 'group', group_id: group.id })
const openDirect = () => directUserId.value ? openRoom({ kind: 'direct', other_user_id: directUserId.value }) : undefined
async function openAssistantApprovalConversation(conversationId: number) {
  const enterpriseId = companyId.value
  activeSection.value = 'chat'
  closeChatWindow()
  if (!enterpriseId) return
  let room = rooms.value.find(item => item.id === conversationId)
  if (!room) {
    try {
      const result = await listEnterpriseConversations(enterpriseId)
      if (enterpriseId !== companyId.value) return
      rooms.value = result.items
      room = result.items.find(item => item.id === conversationId)
    } catch { /* The server will perform the authoritative access check below. */ }
  }
  if (!room) {
    chatError.value = '审批已提交；出于小组会话隔离，你当前无权查看该群聊内容。'
    return
  }
  await selectRoom(room.id)
}
async function selectRoom(id: number) {
  ++openGeneration
  if (roomId.value === id && roomVerified.value) { await refreshMessages(); return }
  rememberRoomDraft()
  teamProgressOpen.value = false
  roomId.value = id; ++roomGeneration; messages.value = []; hasMore.value = false
  fetchedCursor = 0; oldestCursor = 0; messagesInitialized = false
  loadingMessages.value = false; draft.value = ''; roomVerified.value = false; chatError.value = ''
  assistantRun.value = null; assistantRunConversationId.value = null; assistantAnswer.value = ''
  clearAssistantClarification()
  await refreshMessages()
  const accountId = currentUserId.value
  const enterpriseId = companyId.value
  if (accountId && enterpriseId) await restoreAssistantRun(enterpriseId, id, accountId, getToken(), roomGeneration)
}

function invalidateBillingScope() {
  billingScopeInvalid = true
  ++billingContextSequence
  billingContextRequest?.abort()
  billingContextRequest = null
  billingContext.value = null
  billingContextState.value = 'idle'
}
function resetBillingScope() {
  ++billingContextSequence
  billingContextRequest?.abort()
  billingContextRequest = null
  billingContext.value = null
  billingContextState.value = 'idle'
  billingContextToken = getToken()
  billingScopeInvalid = false
  void loadBillingContext()
}
function handleBillingStorage(event: StorageEvent) {
  if ((event.key === 'review_token' || event.key === null) && billingContextToken !== getToken()) invalidateBillingScope()
}
function validBillingContext(value: EnterpriseAssistantBillingContext): boolean {
  if (!value || typeof value.enabled !== 'boolean') return false
  if (value.status === 'group') return Number.isSafeInteger(value.group_id) && (value.group_id ?? 0) > 0
    && typeof value.group_name === 'string' && Boolean(value.group_name.trim())
  return ['unassigned', 'default_group_required'].includes(value.status)
    && value.group_id === null && value.group_name === null
}
async function loadBillingContext() {
  const enterpriseId = companyId.value, conversationId = roomId.value, accountId = currentUserId.value
  const kind = selectedRoom.value?.kind
  if (billingScopeInvalid || !enterpriseId || !conversationId || !accountId || !roomVerified.value
      || selectedRoom.value?.enterprise_id !== enterpriseId || (kind !== 'company' && kind !== 'direct')) return
  if (billingContextToken !== getToken()) { invalidateBillingScope(); return }
  const sequence = ++billingContextSequence, auth = authGeneration, currentRoom = roomGeneration
  billingContextRequest?.abort()
  const request = new AbortController()
  billingContextRequest = request
  const token = billingContextToken
  const current = () => !billingScopeInvalid && sequence === billingContextSequence
    && roomVerified.value && assistantRequestIsCurrent(auth, accountId, token, currentRoom, enterpriseId, conversationId)
  billingContext.value = null
  billingContextState.value = 'loading'
  try {
    const result = await getEnterpriseAssistantBillingContext(enterpriseId, conversationId, token, request.signal)
    if (!current()) return
    if (!validBillingContext(result)) throw new Error('计费归属响应无效')
    billingContext.value = result
    billingContextState.value = 'ready'
  } catch {
    if (current()) billingContextState.value = 'error'
  } finally {
    if (billingContextRequest === request) billingContextRequest = null
    if (!billingScopeInvalid && sequence === billingContextSequence && token !== getToken()) invalidateBillingScope()
  }
}
async function refreshMessages() { await loadMessages(false) }
async function loadOlderMessages() { await loadMessages(true) }
async function loadMessages(older: boolean) {
  const enterpriseId = companyId.value, conversationId = roomId.value, current = roomGeneration
  if (!enterpriseId || !conversationId || loadingRoomGeneration === current || (older && !oldestCursor)) return
  loadingRoomGeneration = current
  loadingMessages.value = true
  const messageList = messagesContainer.value
  const previousHeight = older ? messageList?.scrollHeight ?? 0 : 0
  const previousTop = older ? messageList?.scrollTop ?? 0 : 0
  try {
    const initial = !messagesInitialized
    const result = older
      ? await getEnterpriseMessages(enterpriseId, conversationId, undefined, oldestCursor)
      : await getEnterpriseMessages(enterpriseId, conversationId, initial ? undefined : fetchedCursor)
    if (current !== roomGeneration || enterpriseId !== companyId.value || conversationId !== roomId.value) return
    if (initial && currentUserId.value) {
      draft.value = roomDrafts.get(roomDraftKey(currentUserId.value, enterpriseId, conversationId))?.content || ''
      roomVerified.value = true
    }
    assistantAvailable.value = result.assistant_available
    const seen = new Set(messages.value.map(item => item.id))
    messages.value.push(...result.items.filter(item => !seen.has(item.id)))
    messages.value.sort((a, b) => a.id - b.id)
    if (older || initial) {
      oldestCursor = result.items.reduce((first, item) => first ? Math.min(first, item.id) : item.id, oldestCursor)
      hasMore.value = result.items.length === 100
    }
    if (!older) fetchedCursor = result.items.reduce((last, item) => Math.max(last, item.id), fetchedCursor)
    messagesInitialized = true
    chatError.value = ''
    await nextTick()
    if (current !== roomGeneration) return
    if (older && messageList) messageList.scrollTop = messageList.scrollHeight - previousHeight + previousTop
    else if (initial) scrollToBottom()
    else if (result.items.length && messagesPinned.value) followIfPinned()
  } catch (error) {
    if (current === roomGeneration) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
        clearEnterpriseAccess()
      } else chatError.value = '消息加载失败，请重试。'
    }
  }
  finally {
    if (loadingRoomGeneration === current) loadingRoomGeneration = null
    if (current === roomGeneration) loadingMessages.value = false
  }
}
async function runAssistant(enterpriseId: number, conversationId: number, sourceMessageId: number,
                            accountId: number, token: string | null, current: number) {
  const auth = authGeneration
  assistantRetry.value = { enterpriseId, conversationId, sourceMessageId, accountId, token }
  saveAssistantRecovery(accountId, enterpriseId, conversationId, sourceMessageId)
  try {
    const result = await requestEnterpriseAssistant(enterpriseId, conversationId, sourceMessageId, token)
    if (!assistantRequestIsCurrent(auth, accountId, token, current, enterpriseId, conversationId)) return
    acceptAssistantRun(result, enterpriseId, conversationId, accountId, token)
    chatError.value = ''
    await nextTick()
    if (current === roomGeneration) scrollToBottom()
  } catch (error) {
    if (assistantRequestIsCurrent(auth, accountId, token, current, enterpriseId, conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) {
        assistantRetry.value = null
        clearAssistantRecovery(accountId, enterpriseId, conversationId)
        chatError.value = `消息已发送；${unavailable}`
      } else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
        assistantRetry.value = null
        clearAssistantRecovery(accountId, enterpriseId, conversationId)
        clearEnterpriseAccess()
      } else {
        chatError.value = '小菱请求结果暂未确认；可重新关联原消息查询状态，不会重复发送聊天消息。'
        // A timeout can happen after the server has durably queued the run.
        // Re-query before exposing the idempotent source-message retry action.
        await restoreAssistantRun(enterpriseId, conversationId, accountId, token, current)
      }
    }
  }
}
async function retryAssistant() {
  const pending = assistantRetry.value
  if (!pending || pending.conversationId !== roomId.value || pending.enterpriseId !== companyId.value) return
  if (assistantActionLoading.value) return
  const currentToken = getToken(), currentAuth = authGeneration, currentRoom = roomGeneration
  assistantActionLoading.value = true
  try {
    const result = assistantRun.value?.run_id && assistantRun.value.recoverable
      ? await retryEnterpriseAssistantRun(pending.enterpriseId, pending.conversationId, assistantRun.value.run_id, currentToken)
      : await requestEnterpriseAssistant(pending.enterpriseId, pending.conversationId, pending.sourceMessageId, currentToken)
    if (!assistantRequestIsCurrent(currentAuth, pending.accountId, currentToken, currentRoom,
      pending.enterpriseId, pending.conversationId)) return
    acceptAssistantRun(result, pending.enterpriseId, pending.conversationId, pending.accountId, currentToken)
    chatError.value = ''
  } catch (error) {
    if (assistantRequestIsCurrent(currentAuth, pending.accountId, currentToken, currentRoom,
      pending.enterpriseId, pending.conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) chatError.value = unavailable
      else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else {
        chatError.value = '恢复结果暂未确认；请刷新运行状态后再决定是否重试，系统不会自动重复计费请求。'
        await pollAssistantRun()
      }
    }
  } finally {
    assistantActionLoading.value = false
  }
}
async function answerAssistant() {
  const run = assistantRun.value, enterpriseId = companyId.value, conversationId = roomId.value
  const accountId = currentUserId.value, token = getToken(), currentAuth = authGeneration, currentRoom = roomGeneration
  const answer = assistantAnswer.value.trim()
  if (!run || !run.pending?.call_id || !enterpriseId || !conversationId || !accountId || !answer || assistantActionLoading.value) return
  assistantActionLoading.value = true
  try {
    const result = await answerEnterpriseAssistantRun(enterpriseId, conversationId, run.run_id, run.pending.call_id, answer, token)
    if (!assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) return
    acceptAssistantRun(result, enterpriseId, conversationId, accountId, token)
    chatError.value = ''
  } catch (error) {
    if (assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) chatError.value = unavailable
      else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else { chatError.value = '补充信息提交结果暂未确认；请刷新小菱状态后再继续。'; await pollAssistantRun() }
    }
  } finally { assistantActionLoading.value = false }
}
async function decideAssistantClarification(sourceMessageId: number, decision: EnterpriseAssistantClarificationChoice) {
  const run = assistantRun.value, clarification = assistantClarification.value
  const enterpriseId = companyId.value, conversationId = roomId.value, accountId = currentUserId.value
  const token = getToken(), currentAuth = authGeneration, currentRoom = roomGeneration
  if (!run || !clarification || !clarification.candidates.some(item => item.message_id === sourceMessageId && item.can_decide)
    || !enterpriseId || !conversationId || !accountId || assistantActionLoading.value) return
  assistantActionLoading.value = true
  try {
    await decideEnterpriseAssistantClarification(enterpriseId, conversationId, run.run_id,
      clarification.request_id, sourceMessageId, decision, token)
    if (!assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) return
    const latest = await getEnterpriseAssistantRun(enterpriseId, conversationId, run.run_id, token)
    if (!assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) return
    acceptAssistantRun(latest, enterpriseId, conversationId, accountId, token)
    chatError.value = ''
  } catch (error) {
    if (assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) chatError.value = unavailable
      else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else {
        chatError.value = '权限决定未确认，请刷新当前运行后再继续。'
        await pollAssistantRun()
      }
    }
  } finally { assistantActionLoading.value = false }
}
async function resumeAssistantClarification() {
  const run = assistantRun.value, clarification = assistantClarification.value
  const enterpriseId = companyId.value, conversationId = roomId.value, accountId = currentUserId.value
  const token = getToken(), currentAuth = authGeneration, currentRoom = roomGeneration
  if (!run || !clarification?.can_resume || !enterpriseId || !conversationId || !accountId || assistantActionLoading.value) return
  assistantActionLoading.value = true
  try {
    const result = await resumeEnterpriseAssistantClarification(
      enterpriseId, conversationId, run.run_id, clarification.request_id, token,
    )
    if (!assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) return
    acceptAssistantRun(result, enterpriseId, conversationId, accountId, token)
    chatError.value = ''
  } catch (error) {
    if (assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) chatError.value = unavailable
      else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else {
        chatError.value = '继续请求未确认，请刷新小菱状态后再决定是否恢复。'
        await pollAssistantRun()
      }
    }
  } finally { assistantActionLoading.value = false }
}
async function cancelAssistant() {
  const run = assistantRun.value, enterpriseId = companyId.value, conversationId = roomId.value
  const accountId = currentUserId.value, token = getToken(), currentAuth = authGeneration, currentRoom = roomGeneration
  if (!run || !enterpriseId || !conversationId || !accountId || assistantActionLoading.value) return
  assistantActionLoading.value = true
  try {
    const result = await cancelEnterpriseAssistantRun(enterpriseId, conversationId, run.run_id, '会话成员主动停止', token)
    if (!assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) return
    acceptAssistantRun(result, enterpriseId, conversationId, accountId, token)
    chatError.value = ''
  } catch (error) {
    if (assistantRequestIsCurrent(currentAuth, accountId, token, currentRoom, enterpriseId, conversationId)) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      const unavailable = assistantBillingUnavailable(error)
      if (unavailable) chatError.value = unavailable
      else if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else { chatError.value = '停止请求暂未确认；请刷新运行状态核实。'; await pollAssistantRun() }
    }
  } finally { assistantActionLoading.value = false }
}
async function send() {
  const enterpriseId = companyId.value, conversationId = roomId.value, content = draft.value.trim(), current = roomGeneration
  const accountId = currentUserId.value
  if (!accountId || !enterpriseId || !conversationId || !content || !roomVerified.value || sending.value) return
  const auth = authGeneration
  const token = getToken()
  const roomKey = `${accountId}:${enterpriseId}:${conversationId}`
  sendingRooms.value = new Set(sendingRooms.value).add(roomKey)
  chatError.value = ''
  try {
    const request = await pendingMessageKey(accountId, enterpriseId, conversationId, content)
    if (!assistantRequestIsCurrent(auth, accountId, token, current, enterpriseId, conversationId)) return
    const result = await sendEnterpriseMessage(enterpriseId, conversationId, content, request.id, token)
    clearPendingMessage(request.key)
    if (auth === authGeneration && currentUserId.value === accountId && token === getToken()
      && roomDrafts.get(roomKey)?.content.trim() === content) roomDrafts.delete(roomKey)
    if (!assistantRequestIsCurrent(auth, accountId, token, current, enterpriseId, conversationId)) return
    assistantAvailable.value = result.assistant_available
    if (!messages.value.some(item => item.id === result.message.id)) messages.value.push(result.message)
    if (draft.value.trim() === content) draft.value = ''
    await nextTick()
    if (current === roomGeneration) scrollToBottom()
    if (content.includes('@小菱') && !result.assistant_available && current === roomGeneration) {
      assistantRetry.value = { enterpriseId, conversationId, sourceMessageId: result.message.id, accountId, token }
      saveAssistantRecovery(accountId, enterpriseId, conversationId, result.message.id)
      chatError.value = '消息已发送；企业订阅、积分或平台计量条件未满足，小菱没有启动。'
    }
    if (content.includes('@小菱') && result.assistant_available && auth === authGeneration
      && currentUserId.value === accountId && token === getToken()) {
      await runAssistant(enterpriseId, conversationId, result.message.id, accountId, token, current)
    }
  } catch (error) {
    if (current === roomGeneration) {
      const code = typeof error === 'object' && error !== null && 'code' in error ? Number(error.code) : 0
      if ((code >= 40300 && code < 40500) || code === 403 || code === 404) clearEnterpriseAccess()
      else chatError.value = '消息发送失败，请重试。'
    }
  }
  finally {
    if (auth === authGeneration) {
      const next = new Set(sendingRooms.value)
      next.delete(roomKey)
      sendingRooms.value = next
    }
  }
}
watch([currentUserId, () => user.token], ([current, token], [previous, previousToken]) => {
  if (current === previous && token === previousToken) return
  const active = document.activeElement
  const restoreFocusToEnterprise = Boolean(enterprisePageRef.value?.contains(active)
    || chatPanelRef.value?.contains(active))
  ++authGeneration
  ++companyGeneration; ++roomGeneration; ++openGeneration
  companies.value = []; companyId.value = null; roomId.value = null
  rooms.value = []; groups.value = []; directory.value = []; messages.value = []; draft.value = ''; roomVerified.value = false
  roomDrafts.clear()
  hasMore.value = false; directoryHasMore.value = false; fetchedCursor = 0
  joining.value = false; loadingCompanies.value = false
  invitation.value = ''; showInvitation.value = false; resourcesStale = false
  roomFilter.value = ''; chatFloating.value = false
  proactiveSettingsOpen.value = false
  chatWindowReturnFocus.value = null
  assistantAvailable.value = false
  assistantRetry.value = null
  assistantRun.value = null; assistantRunConversationId.value = null; assistantAnswer.value = ''
  clearAssistantClarification()
  assistantActionLoading.value = false
  lastWorkspaceRefreshAt = 0
  lastResumeRefreshAt = Number.NEGATIVE_INFINITY
  sendingRooms.value = new Set(); pendingIds.clear(); pageError.value = ''; chatError.value = ''
  if (restoreFocusToEnterprise) {
    void nextTick(() => {
      const currentFocus = document.activeElement
      if (currentFocus && currentFocus !== document.body && currentFocus.isConnected) return
      enterpriseHeadingRef.value?.focus({ preventScroll: true })
    })
  }
  if (current) void loadCompanies()
}, { flush: 'sync' })
watch(() => [currentUserId.value, user.token, companyId.value, roomId.value, roomVerified.value, selectedRoom.value?.kind] as const,
  resetBillingScope, { flush: 'sync' })
watch(canManageGroups, allowed => {
  if (!allowed && (activeSection.value === 'management' || activeSection.value === 'usage')) activeSection.value = 'chat'
})
watch(canManageCompany, allowed => {
  if (!allowed) proactiveSettingsOpen.value = false
  if (!allowed && activeSection.value === 'assistant-approvals') activeSection.value = 'chat'
})
onMounted(() => {
  window.addEventListener('keydown', handleFloatingWindowKeydown)
  window.addEventListener('focus', refreshOnResume)
  window.addEventListener('storage', handleBillingStorage)
  window.addEventListener('prism:auth-expired', invalidateBillingScope)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  void loadCompanies()
  lastWorkspaceRefreshAt = Date.now()
  poll = setInterval(() => { void refreshVisibleState() }, 5000)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleFloatingWindowKeydown)
  window.removeEventListener('focus', refreshOnResume)
  window.removeEventListener('storage', handleBillingStorage)
  window.removeEventListener('prism:auth-expired', invalidateBillingScope)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  ++authGeneration; ++companyGeneration; ++roomGeneration; ++openGeneration; invalidateBillingScope()
  if (poll) clearInterval(poll)
})
</script>

<style scoped>
.enterprise-page { max-width: 1360px; margin: auto; padding: 26px; color: #20243a; }
.page-header, .workspace-top, .header-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-header h1 { margin: 0; font-size: 24px; }
.muted, .empty p { color: #69718a; font-size: 13px; line-height: 1.6; }
button, select, input, textarea { font: inherit; border: 1px solid #d9ddeb; background: #fff; border-radius: 8px; padding: 8px 10px; color: inherit; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid #7379ec; outline-offset: 2px; }
.workspace-top { justify-content: flex-start; margin: 22px 0 14px; font-size: 14px; }
.workspace-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.workspace-tabs [aria-pressed='true'] { background: #eff0ff; color: #424cc5; border-color: #cfd2ff; }
.card, .notice { background: #fff; border: 1px solid #e9eaf3; border-radius: 14px; padding: 18px; }
.notice { margin-top: 16px; }.empty { padding: 30px 18px; text-align: center; }
.join { display: flex; gap: 8px; align-items: center; justify-content: center; flex-wrap: wrap; }
.chat-shell { display: grid; grid-template-columns: 292px minmax(0, 1fr); min-height: min(720px, calc(100vh - 255px)); overflow: hidden; border: 1px solid #e4e7ed; border-radius: 14px; background: #f3f5f7; box-shadow: 0 8px 28px rgb(25 36 52 / 6%); }
.chat-shell.floating-only { display: none; }
.conversation-list { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: #fff; border-right: 1px solid #e7e9ee; }
.conversation-list-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 15px 14px 10px; }
.proactive-toggle { margin: 0 8px 6px; padding: 7px; border: 0; border-radius: 8px; background: transparent; color: #555d6d; text-align: left; font-size: 13px; }
.proactive-toggle:hover { background: #f5f6f8; }
.conversation-list-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.conversation-list-title h2 { overflow: hidden; margin: 0; font-size: 15px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.conversation-list-title p { margin: 3px 0 0; color: #9299a7; font-size: 11px; }
.enterprise-avatar, .room-avatar, .message-avatar, .welcome-avatar { display: grid; flex: none; place-items: center; border-radius: 10px; background: #e9eeff; color: #4b5dc2; font-weight: 650; }
.enterprise-avatar { width: 40px; height: 40px; background: #e4edff; }
.icon-button { display: grid; width: 34px; height: 34px; flex: none; place-items: center; padding: 0; border: 0; border-radius: 8px; background: transparent; color: #71798a; font-size: 20px; }
.icon-button:hover { background: #f0f2f5; }.refresh-icon::before { content: '↻'; }
.conversation-search { display: flex; align-items: center; gap: 7px; margin: 5px 12px 12px; padding: 0 9px; border: 1px solid #edf0f3; border-radius: 7px; background: #f5f6f8; color: #9299a7; }
.conversation-search input { width: 100%; min-width: 0; padding: 8px 0; border: 0; outline: 0; background: transparent; font-size: 12px; }
.conversation-quick-actions { display: grid; gap: 2px; padding: 0 8px 10px; }
.conversation-quick-actions button { display: grid; grid-template-columns: 36px minmax(0, 1fr) auto; align-items: center; gap: 9px; min-width: 0; padding: 7px; border: 0; border-radius: 8px; background: #fff; text-align: left; font-size: 13px; }
.conversation-quick-actions button:hover { background: #f5f6f8; }.room-arrow::after { content: '›'; color: #9ca3af; }
.room-avatar { width: 38px; height: 38px; border-radius: 9px; background: #edf1f7; color: #647086; font-size: 14px; }
.company-avatar, .avatar-company { background: #e5efff; color: #4e6eb7; }
.company-avatar::before { content: '企'; }
.group-avatar, .avatar-group { background: #e6f5ee; color: #45876a; }
.group-avatar::before { content: '组'; }
.avatar-direct { background: #fff0df; color: #af7945; }
.conversation-divider { display: flex; align-items: center; justify-content: space-between; padding: 7px 15px; border-top: 1px solid #f0f1f4; color: #999fac; font-size: 11px; }
.conversation-divider button { padding: 3px 5px; border: 0; color: #6d73cc; font-size: 11px; }
.conversation-empty { margin: 8px 12px; border: 0; background: #f7f8fa; color: #8991a0; font-size: 12px; line-height: 1.5; text-align: left; }
.room-row { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; align-items: center; gap: 9px; width: 100%; min-width: 0; padding: 9px 12px; border: 0; border-radius: 0; background: #fff; text-align: left; }
.room-row:hover { background: #f7f8fa; }.room-row.selected { background: #edf2f8; }
.room-copy { display: grid; min-width: 0; gap: 5px; }
.room-title, .room-preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-title { color: #272b33; font-size: 13px; }.room-preview { color: #939aa7; font-size: 11px; }
.room-kind { align-self: start; padding-top: 2px; color: #adb3bd; font-size: 10px; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.direct-start { display: grid; gap: 7px; margin: auto 12px 12px; padding-top: 12px; border-top: 1px solid #eceef2; font-size: 12px; }
.direct-start > div { display: flex; gap: 6px; }.direct-start select { flex: 1; min-width: 0; padding: 7px; font-size: 12px; }.direct-start button { padding: 7px 10px; background: #f1f2ff; border-color: #dfe1ff; color: #555cc1; font-size: 12px; }
.conversation-stage { position: relative; min-width: 0; min-height: 0; }
.chat-pane { position: relative; z-index: 1; display: flex; box-sizing: border-box; width: 100%; height: 100%; min-width: 0; min-height: 560px; flex-direction: column; overflow: hidden; background: #f5f6f8; }
.chat-pane.is-floating { position: fixed; z-index: 4101; width: min(400px, calc(100vw - 24px)); height: min(620px, calc(100dvh - 24px)); min-height: 0; border: 1px solid #d9dde5; border-radius: 14px; box-shadow: 0 18px 55px rgb(18 28 45 / 25%); }
.chat-pane-header { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; grid-template-rows: auto auto; align-items: center; gap: 0 9px; padding: 12px 16px 10px; border-bottom: 1px solid #e7e9ee; background: #fff; }
.mobile-back, .chat-drag-handle { display: none; width: 32px; height: 32px; place-items: center; padding: 0; border: 0; background: transparent; color: #626b7a; font-size: 22px; }
.header-avatar { grid-column: 1; grid-row: 1; width: 38px; height: 38px; }
.chat-title-block { grid-column: 2; grid-row: 1; min-width: 0; }
.chat-title-block h2 { overflow: hidden; margin: 0; color: #242933; font-size: 15px; text-overflow: ellipsis; white-space: nowrap; }
.chat-title-block p { margin: 4px 0 0; color: #9299a5; font-size: 11px; }
.chat-toolbar { display: flex; grid-column: 3; grid-row: 1; gap: 4px; }
.team-progress-toggle { min-height: 32px; padding: 0 8px; border: 1px solid #dfe5ee; border-radius: 8px; background: #fff; color: #536985; font-size: 11px; white-space: nowrap; }
.team-progress-toggle[aria-pressed="true"] { border-color: #87a4cf; background: #edf4ff; color: #355f9b; }
.team-progress-toggle:focus-visible { outline: 2px solid #6f98d2; outline-offset: 2px; }
.is-floating .chat-pane-header { grid-template-columns: 30px 34px 42px minmax(0, 1fr) auto; }
.is-floating .chat-drag-handle { display: grid; grid-column: 2; grid-row: 1; cursor: grab; touch-action: none; }
.is-floating .chat-drag-handle:active { cursor: grabbing; }
.is-floating .header-avatar { grid-column: 3; }
.is-floating .chat-title-block { grid-column: 4; }
.is-floating .chat-toolbar { grid-column: 5; }
.chat-inline-error { margin: 8px 14px 0; padding: 8px 10px; border: 1px solid #f1d5d5; border-radius: 8px; background: #fff4f4; color: #a33f3f; font-size: 12px; }
.assistant-run-card { display: grid; flex: none; gap: 7px; margin: 8px 14px; padding: 10px 12px; border: 1px solid #e1e3f2; border-radius: 10px; background: #fff; color: #545d70; font-size: 12px; }
.assistant-run-heading, .assistant-run-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.assistant-run-heading strong { color: #514ea2; font-size: 12px; }.assistant-run-heading span { color: #8b91a1; }
.assistant-run-card > p { margin: 0; line-height: 1.5; }.assistant-run-actions button, .assistant-answer button { padding: 6px 10px; font-size: 11px; }
.assistant-run-actions .assistant-cancel { color: #a23f3f; }
.assistant-answer { display: grid; gap: 6px; }.assistant-answer label { color: #727a89; }
.assistant-answer textarea { resize: vertical; border-color: #e4e7ed; font-size: 12px; line-height: 1.5; }
.assistant-run-error { color: #a23f3f; }
.assistant-clarification { display: grid; gap: 8px; max-height: min(32vh, 240px); overflow-y: auto; overscroll-behavior: contain; }
.assistant-clarification > p { margin: 0; line-height: 1.5; }
.assistant-clarification-action { color: #303641; font-weight: 600; }
.clarification-candidate { display: grid; gap: 5px; padding: 8px 9px; border-radius: 8px; background: #f6f7fa; }
.clarification-candidate blockquote { margin: 0; color: #414854; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.clarification-candidate-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: #858b96; font-size: 11px; }
.clarification-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.clarification-actions button { min-height: 28px; padding: 4px 8px; border: 1px solid #e1e5eb; border-radius: 6px; background: #fff; color: #525d6d; font-size: 11px; }
.clarification-actions .clarification-resume { border-color: #4f70cb; background: #4f70cb; color: #fff; }
.assistant-clarification .clarification-blocked { color: #9d5555; }
.message-list { display: flex; min-height: 0; flex: 1; flex-direction: column; gap: 14px; overflow: auto; padding: 18px max(18px, 4vw); overscroll-behavior: contain; }
.load-older { align-self: center; border: 0; background: transparent; color: #778197; font-size: 12px; }
.message-empty { align-self: center; margin: auto; color: #9aa1ae; font-size: 13px; text-align: center; }
.message-row { display: flex; align-items: flex-start; gap: 9px; max-width: min(760px, 88%); }
.message-row.mine { align-self: flex-end; justify-content: flex-end; }
.message-avatar { width: 32px; height: 32px; border-radius: 8px; background: #e7eaf0; color: #6f7889; font-size: 12px; }
.assistant-avatar { background: #ecebff; color: #5d56b2; }
.mine-avatar { order: 2; background: #dce9ff; color: #526ba6; }
.message-body { display: grid; min-width: 0; justify-items: start; gap: 4px; }
.mine .message-body { justify-items: end; }
.sender { max-width: 100%; overflow: hidden; color: #737c8b; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.message-bubble { max-width: 100%; margin: 0; padding: 10px 12px; border: 1px solid #eaecf0; border-radius: 4px 12px 12px; background: #fff; color: #303641; font-size: 13px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.mine .message-bubble { border-color: #dce6fa; border-radius: 12px 4px 12px 12px; background: #dcecff; }
.assistant .message-bubble { border-color: #e4e1ff; background: #fbfaff; }
.message-time { color: #a0a6b0; font-size: 10px; }
.message-composer { display: grid; flex: none; gap: 7px; padding: 10px 14px 7px; border-top: 1px solid #e7e9ee; background: #fff; }
.message-composer textarea { box-sizing: border-box; width: 100%; min-width: 0; min-height: 76px; max-height: 190px; resize: vertical; padding: 10px 11px; border-color: #e4e7ed; border-radius: 9px; background: #fbfbfc; font-size: 13px; line-height: 1.5; }
.assistant-billing-context { margin: 0; color: #596a80; font-size: 12px; }
.assistant-billing-context button { margin-left: 6px; padding: 0; border: 0; background: transparent; color: #365bb2; text-decoration: underline; }
.composer-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: #a0a6b0; font-size: 10px; }
.composer-footer button, .chat-welcome button { padding: 7px 16px; border: 0; border-radius: 7px; background: #4f70cb; color: #fff; font-size: 12px; }
.assistant-status-note { flex: none; margin: 0; padding: 0 14px 9px; background: #fff; color: #596a80; font-size: 12px; }
.chat-welcome { display: grid; place-content: center; justify-items: center; gap: 10px; height: 100%; min-height: 420px; color: #838b98; text-align: center; }
.chat-welcome h2 { margin: 2px 0 0; color: #465064; font-size: 18px; }.chat-welcome p { margin: 0; font-size: 13px; }
.welcome-avatar { width: 58px; height: 58px; border-radius: 16px; background: #e5edff; font-size: 22px; }
.error { color: #a23434; font-size: 13px; }.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }
@media (max-width: 900px) { .enterprise-page { padding: 20px; }.chat-shell { grid-template-columns: 252px minmax(0, 1fr); }.conversation-list-head { padding-inline: 10px; } }
@media (max-width: 760px) {
  .enterprise-page { padding: 16px; }.page-header { align-items: flex-start; flex-direction: column; }
  .chat-shell { grid-template-columns: minmax(0, 1fr); min-height: calc(100dvh - 300px); }
  .chat-shell.has-room .conversation-list { display: none; }.chat-shell:not(.has-room) .conversation-stage { display: none; }
  .conversation-list { border-right: 0; }.conversation-list-head { padding-inline: 14px; }
  .chat-pane { min-height: 460px; }.message-list { padding: 14px; }.message-row { max-width: 95%; }
  .chat-pane-header { grid-template-columns: 34px 38px minmax(0, 1fr) auto; padding: 10px; }
  .mobile-back { display: grid; grid-column: 1; grid-row: 1; }
  .header-avatar { grid-column: 2; }.chat-title-block { grid-column: 3; }.chat-toolbar { grid-column: 4; }
  .is-floating .chat-pane-header { grid-template-columns: 34px 38px minmax(0, 1fr) auto; }
  .is-floating .chat-drag-handle { display: none; }.is-floating .header-avatar { grid-column: 2; }.is-floating .chat-title-block { grid-column: 3; }.is-floating .chat-toolbar { grid-column: 4; }
  .chat-pane.is-floating { inset: 0 !important; width: 100vw !important; height: 100dvh !important; max-width: none; border: 0; border-radius: 0; }
  .chat-pane.is-floating :deep(.panel-resize-handles) { display: none; }
}
@media (max-height: 520px) and (min-width: 761px) {
  .chat-pane.is-floating { inset: 0 !important; width: 100vw !important; height: 100dvh !important; max-width: none; border: 0; border-radius: 0; }
  .chat-pane.is-floating :deep(.panel-resize-handles) { display: none; }
  .is-floating .chat-pane-header { grid-template-columns: 34px 38px minmax(0, 1fr) auto; }
  .is-floating .chat-drag-handle { display: none; }
  .is-floating .header-avatar { grid-column: 2; }.is-floating .chat-title-block { grid-column: 3; }.is-floating .chat-toolbar { grid-column: 4; }
}
</style>
