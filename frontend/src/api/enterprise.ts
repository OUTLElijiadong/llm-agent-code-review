import { del, get, post, put } from './http'

export interface Enterprise {
  id: number
  name: string
  status: 'active' | 'suspended'
  role: 'owner' | 'admin' | 'member'
  default_group_id: number | null
  seat_limit?: number
  active_member_count?: number
  subscription_status?: 'active' | 'grace' | 'expired' | 'none'
  subscription_ends_at?: string | null
  over_capacity?: boolean
}

export interface EnterpriseGroup {
  id: number
  enterprise_id: number
  name: string
  status: string
  my_role?: 'leader' | 'member' | null
}

export interface EnterpriseGroupMember { user_id: number; role: 'leader' | 'member' }
export interface EnterpriseInvitation { id: number; created_by: number; expires_at: string; use_count: number; revoked_at: string | null }

export interface EnterpriseMember {
  user_id: number
  role: string
  status: string
  default_group_id: number | null
}

export interface EnterpriseDirectoryMember {
  user_id: number
  display_name: string
}

export interface EnterpriseConversation {
  id: number
  enterprise_id: number
  kind: 'company' | 'group' | 'direct'
  group_id: number | null
  participant_low_id: number | null
  participant_high_id: number | null
  create_time: string
}

/** 仅为当前登录者提供预计归属，不代表付款或执行授权。 */
export interface EnterpriseAssistantBillingContext {
  status: 'group' | 'unassigned' | 'default_group_required'
  group_id: number | null
  group_name: string | null
  enabled: boolean
}

export interface EnterpriseProjectSummary {
  project_id: number
  project_name: string
  description?: string
  enterprise_id: number
  group_id: number
  group_name: string
  role: 'admin' | 'leader' | 'editor' | 'reviewer' | 'viewer'
}

export interface EnterpriseRemoteApproval {
  id: number
  enterprise_id: number
  group_id: number
  project_id: number | null
  owner_id: number
  target_url: string
  target_host: string
  authorization_basis: string
  scope_notes: string
  requested_window_hours: number
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  decided_by: number | null
  decision_reason: string | null
  decided_at: string | null
  create_time: string
  execution_task_public_id: string | null
  execution_available: boolean
}

export interface EnterpriseMessage {
  id: number
  conversation_id: number
  sender_id: number | null
  sender_type: 'human' | 'assistant'
  assistant_run_id?: number | null
  client_message_id: string
  content: string
  create_time: string
}

export type EnterpriseAssistantStatus =
  | 'queued' | 'running' | 'waiting_approval' | 'waiting_input' | 'awaiting_clarification' | 'completed'
  | 'failed' | 'incomplete' | 'max_rounds_exceeded' | 'cancelled' | 'retryable'
  | 'suppressed' | 'awaiting_reconciliation'

export type EnterpriseAssistantClarificationChoice = 'allow' | 'deny' | 'unrelated'

export interface EnterpriseAssistantClarificationCandidate {
  message_id: number
  author_id: number
  rank: number
  content: string
  decision: EnterpriseAssistantClarificationChoice | 'pending'
  can_decide: boolean
}

export interface EnterpriseAssistantClarification {
  request_id: number
  version: number
  call_id: string
  action: string
  title: string | null
  candidates: EnterpriseAssistantClarificationCandidate[]
  status: 'waiting' | 'complete' | 'denied' | 'stale' | 'expired' | 'cancelled'
  can_resume: boolean
}

export interface EnterpriseAssistantPending {
  kind: 'approval' | 'input' | 'clarification'
  call_id: string
  operation: string
  approval_id: number | null
  question: string | null
  preview?: Pick<EnterpriseAssistantClarification, 'request_id' | 'version' | 'action' | 'title'> | null
}

export interface EnterpriseAssistantRun {
  assistant_mode: 'enterprise_agent_runtime'
  run_id: string
  status: EnterpriseAssistantStatus
  message: EnterpriseMessage | null
  pending: EnterpriseAssistantPending | null
  error?: string | null
  recoverable?: boolean
  initiator_user_id: number
  source_message_id?: number
}

export interface EnterpriseAssistantApproval {
  id: number
  run_id: string
  conversation_id: number
  group_id: number | null
  initiator_user_id: number
  required_role: string
  tool: string
  title: string | null
  content: string | null
  status: string
  create_time: string
}

export interface EnterpriseContent {
  id: number
  enterprise_id: number
  author_id: number
  title: string
  content: string
  create_time: string
  update_time: string
}

export interface EnterpriseReply {
  id: number
  enterprise_id: number
  post_id: number
  author_id: number
  content: string
  create_time: string
}

export const listMyEnterprises = () => get<Enterprise[]>('/enterprises/mine')
export const acceptEnterpriseInvitation = (token: string) =>
  post<{ enterprise_id: number }>('/enterprises/invitations/accept', { token })
export const listEnterpriseGroups = (enterpriseId: number, token?: string | null) =>
  token === undefined ? get<EnterpriseGroup[]>(`/enterprises/${enterpriseId}/groups`)
    : get<EnterpriseGroup[]>(`/enterprises/${enterpriseId}/groups`, undefined, undefined, false, { authTokenSnapshot: token })
export const listEnterpriseProjects = (enterpriseId: number) =>
  get<EnterpriseProjectSummary[]>(`/enterprises/${enterpriseId}/projects`, undefined, [40300, 40301, 40303, 40400], true)
export const listEnterpriseRemoteApprovals = (enterpriseId: number, limit = 20, offset = 0) =>
  get<{ items: EnterpriseRemoteApproval[]; total: number }>(
    `/enterprises/${enterpriseId}/remote-test-requests`, { limit, offset }, [40300, 40301, 40303, 40400], true,
  )
export const submitEnterpriseRemoteApproval = (
  enterpriseId: number,
  input: {
    group_id: number
    project_id: number
    target_url: string
    authorization_basis: string
    scope_notes: string
    requested_window_hours: number
    ack_rules: true
  },
  token: string | null,
) => post<EnterpriseRemoteApproval>(
  `/enterprises/${enterpriseId}/remote-test-requests`, input,
  undefined, [40200, 40300, 40301, 40303, 40400, 40901], true, { authTokenSnapshot: token },
)
export const decideEnterpriseRemoteApproval = (
  enterpriseId: number, requestId: number, decision: 'approved' | 'rejected', reason: string, token: string | null,
) => post<EnterpriseRemoteApproval>(
  `/enterprises/${enterpriseId}/remote-test-requests/${requestId}/decision`, { decision, reason },
  undefined, [40200, 40300, 40301, 40303, 40400, 40901], true, { authTokenSnapshot: token },
)
export const cancelEnterpriseRemoteApproval = (enterpriseId: number, requestId: number, token: string | null) =>
  post<EnterpriseRemoteApproval>(
    `/enterprises/${enterpriseId}/remote-test-requests/${requestId}/cancel`, {},
    undefined, [40200, 40300, 40301, 40303, 40400, 40901], true, { authTokenSnapshot: token },
  )
export const startEnterpriseRemoteApproval = (enterpriseId: number, requestId: number, token: string | null) =>
  post<{ request: EnterpriseRemoteApproval; execution: { public_id: string; status: string } }>(
    `/enterprises/${enterpriseId}/remote-test-requests/${requestId}/start`, {},
    undefined, [40200, 40300, 40301, 40303, 40400, 40901], true, { authTokenSnapshot: token },
  )
export const listEnterpriseMembers = (enterpriseId: number, token?: string | null) =>
  token === undefined ? get<EnterpriseMember[]>(`/enterprises/${enterpriseId}/members`)
    : get<EnterpriseMember[]>(`/enterprises/${enterpriseId}/members`, undefined, undefined, false, { authTokenSnapshot: token })
export const createEnterpriseInvitation = (enterpriseId: number, ttlHours = 24) =>
  post<{ token: string; expires_in_hours: number }>(`/enterprises/${enterpriseId}/invitations`, { ttl_hours: ttlHours })
export const listEnterpriseInvitations = (enterpriseId: number, token?: string | null) =>
  token === undefined ? get<EnterpriseInvitation[]>(`/enterprises/${enterpriseId}/invitations`)
    : get<EnterpriseInvitation[]>(`/enterprises/${enterpriseId}/invitations`, undefined, undefined, false, { authTokenSnapshot: token })
export const revokeEnterpriseInvitation = (enterpriseId: number, invitationId: number) =>
  del<null>(`/enterprises/${enterpriseId}/invitations/${invitationId}`)
export const createEnterpriseGroup = (enterpriseId: number, name: string) =>
  post<EnterpriseGroup>(`/enterprises/${enterpriseId}/groups`, { name })
export const setEnterpriseMemberRole = (enterpriseId: number, userId: number, role: 'admin' | 'member') =>
  put<EnterpriseMember>(`/enterprises/${enterpriseId}/members/${userId}/role`, { role })
export const setEnterpriseMemberDefaultGroup = (enterpriseId: number, userId: number, groupId: number | null,
  token: string | null) =>
  put<EnterpriseMember>(`/enterprises/${enterpriseId}/members/${userId}/default-group`,
    { group_id: groupId }, { authTokenSnapshot: token })
export const removeEnterpriseMember = (enterpriseId: number, userId: number) =>
  del<null>(`/enterprises/${enterpriseId}/members/${userId}`)
export const listEnterpriseGroupMembers = (enterpriseId: number, groupId: number, token?: string | null) =>
  get<EnterpriseGroupMember[]>(`/enterprises/${enterpriseId}/groups/${groupId}/members`, undefined, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
export const setEnterpriseGroupMember = (enterpriseId: number, groupId: number, userId: number, role: 'leader' | 'member') =>
  put<EnterpriseGroupMember>(`/enterprises/${enterpriseId}/groups/${groupId}/members`, { user_id: userId, role })
export const removeEnterpriseGroupMember = (enterpriseId: number, groupId: number, userId: number) =>
  del<null>(`/enterprises/${enterpriseId}/groups/${groupId}/members/${userId}`)
export const listEnterpriseDirectory = (enterpriseId: number, offset = 0, token?: string | null) =>
  token === undefined ? get<EnterpriseDirectoryMember[]>(`/enterprises/${enterpriseId}/directory`, { offset, limit: 100 })
    : get<EnterpriseDirectoryMember[]>(`/enterprises/${enterpriseId}/directory`, { offset, limit: 100 },
      undefined, false, { authTokenSnapshot: token })
export const listEnterpriseConversations = (enterpriseId: number) =>
  get<{ items: EnterpriseConversation[]; assistant_available: boolean }>(`/enterprises/${enterpriseId}/conversations`)
export const openEnterpriseConversation = (enterpriseId: number, input:
  { kind: 'company' | 'group' | 'direct'; group_id?: number; other_user_id?: number }) =>
  post<EnterpriseConversation>(`/enterprises/${enterpriseId}/conversations`, input)
export const getEnterpriseMessages = (enterpriseId: number, conversationId: number, afterId?: number, beforeId?: number) =>
  get<{ items: EnterpriseMessage[]; assistant_available: boolean }>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/messages`,
    { limit: 100, ...(afterId === undefined ? {} : { after_id: afterId }),
      ...(beforeId === undefined ? {} : { before_id: beforeId }) },
    [40300, 40301, 40303, 40400], true,
  )
export const getEnterpriseAssistantBillingContext = (enterpriseId: number, conversationId: number,
  token: string | null, signal: AbortSignal) =>
  get<EnterpriseAssistantBillingContext>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-billing-context`,
    undefined, [40300, 40301, 40303, 40400], true, { authTokenSnapshot: token, timeout: 15000, signal },
  )
export const sendEnterpriseMessage = (enterpriseId: number, conversationId: number, content: string, clientMessageId: string, token: string | null) =>
  post<{ message: EnterpriseMessage; assistant_available: boolean }>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/messages`,
    { content, client_message_id: clientMessageId },
    undefined, undefined, false, { authTokenSnapshot: token },
  )
export const requestEnterpriseAssistant = (enterpriseId: number, conversationId: number, sourceMessageId: number, token: string | null) =>
  post<EnterpriseAssistantRun>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs`,
    { source_message_id: sourceMessageId }, undefined, [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const listEnterpriseAssistantRuns = (enterpriseId: number, conversationId: number) =>
  get<{ items: EnterpriseAssistantRun[]; assistant_mode: 'enterprise_agent_runtime' }>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs`,
    undefined, [40300, 40301, 40303, 40400], true,
  )
export const getEnterpriseAssistantRun = (enterpriseId: number, conversationId: number, runId: string,
                                          token?: string | null) =>
  get<EnterpriseAssistantRun>(`/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}`,
    undefined, [40300, 40301, 40303, 40400], true,
    token === undefined ? undefined : { authTokenSnapshot: token })
export const getEnterpriseAssistantClarification = (enterpriseId: number, conversationId: number, runId: string,
                                                    token: string | null) =>
  get<EnterpriseAssistantClarification>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/clarification`,
    undefined, [40300, 40301, 40303, 40400, 40901], true, { authTokenSnapshot: token },
  )
export const decideEnterpriseAssistantClarification = (enterpriseId: number, conversationId: number, runId: string,
                                                       requestId: number, sourceMessageId: number,
                                                       decision: EnterpriseAssistantClarificationChoice,
                                                       token: string | null) =>
  post<{ request_id: number; status: EnterpriseAssistantClarification['status'] }>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/clarification/${requestId}/decision`,
    { source_message_id: sourceMessageId, decision }, undefined, [40300, 40301, 40303, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const resumeEnterpriseAssistantClarification = (enterpriseId: number, conversationId: number, runId: string,
                                                       requestId: number, token: string | null) =>
  post<EnterpriseAssistantRun>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/clarification/${requestId}/resume`,
    {}, undefined, [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const answerEnterpriseAssistantRun = (enterpriseId: number, conversationId: number, runId: string,
                                             callId: string, answer: string, token: string | null) =>
  post<EnterpriseAssistantRun>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/answer`,
    { call_id: callId, answer }, undefined, [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const cancelEnterpriseAssistantRun = (enterpriseId: number, conversationId: number, runId: string,
                                             reason: string, token: string | null) =>
  post<EnterpriseAssistantRun>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/cancel`,
    { reason }, undefined, [40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const retryEnterpriseAssistantRun = (enterpriseId: number, conversationId: number, runId: string, token: string | null) =>
  post<EnterpriseAssistantRun>(
    `/enterprises/${enterpriseId}/conversations/${conversationId}/assistant-runs/${encodeURIComponent(runId)}/retry`,
    {}, undefined, [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const listEnterpriseAssistantApprovals = (enterpriseId: number) =>
  get<{ items: EnterpriseAssistantApproval[]; assistant_mode: 'enterprise_agent_runtime' }>(
    `/enterprises/${enterpriseId}/conversations/assistant-approvals`, undefined, [40300, 40301, 40303, 40400], true,
  )
export const decideEnterpriseAssistantApproval = (enterpriseId: number, approvalId: number,
                                                   decision: 'approve' | 'reject', reason: string, token: string | null) =>
  post<EnterpriseAssistantRun & { approval_id: number; decision: 'approve' | 'reject' }>(
    `/enterprises/${enterpriseId}/conversations/assistant-approvals/${approvalId}/decision`,
    { decision, reason }, undefined, [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901], true,
    { authTokenSnapshot: token },
  )
export const listEnterprisePosts = (enterpriseId: number, beforeId?: number) =>
  get<{ items: EnterpriseContent[] }>(`/enterprises/${enterpriseId}/forum/posts`, beforeId ? { before_id: beforeId } : {})
export const createEnterprisePost = (enterpriseId: number, title: string, content: string, requestKey: string, token: string | null) =>
  post<EnterpriseContent>(`/enterprises/${enterpriseId}/forum/posts`, { title, content, request_key: requestKey },
    undefined, undefined, false, { authTokenSnapshot: token })
export const getEnterprisePost = (enterpriseId: number, postId: number, afterReplyId = 0) =>
  get<{ post: EnterpriseContent; replies: EnterpriseReply[] }>(
    `/enterprises/${enterpriseId}/forum/posts/${postId}`, { after_reply_id: afterReplyId },
  )
export const updateEnterprisePost = (enterpriseId: number, postId: number, title: string, content: string, token: string | null) =>
  put<EnterpriseContent>(`/enterprises/${enterpriseId}/forum/posts/${postId}`, { title, content }, { authTokenSnapshot: token })
export const deleteEnterprisePost = (enterpriseId: number, postId: number, token: string | null) =>
  del<null>(`/enterprises/${enterpriseId}/forum/posts/${postId}`, { authTokenSnapshot: token })
export const replyEnterprisePost = (enterpriseId: number, postId: number, content: string, requestKey: string, token: string | null) =>
  post<EnterpriseReply>(`/enterprises/${enterpriseId}/forum/posts/${postId}/replies`, { content, request_key: requestKey },
    undefined, undefined, false, { authTokenSnapshot: token })
export const deleteEnterpriseReply = (enterpriseId: number, postId: number, replyId: number, token: string | null) =>
  del<null>(`/enterprises/${enterpriseId}/forum/posts/${postId}/replies/${replyId}`, { authTokenSnapshot: token })
export const listEnterpriseAnnouncements = (enterpriseId: number, beforeId?: number) =>
  get<{ items: EnterpriseContent[] }>(`/enterprises/${enterpriseId}/announcements`, beforeId ? { before_id: beforeId } : {})
export const createEnterpriseAnnouncement = (enterpriseId: number, title: string, content: string, requestKey: string, token: string | null) =>
  post<EnterpriseContent>(`/enterprises/${enterpriseId}/announcements`, { title, content, request_key: requestKey },
    undefined, undefined, false, { authTokenSnapshot: token })
export const updateEnterpriseAnnouncement = (enterpriseId: number, announcementId: number, title: string, content: string, token: string | null) =>
  put<EnterpriseContent>(`/enterprises/${enterpriseId}/announcements/${announcementId}`, { title, content }, { authTokenSnapshot: token })
export const deleteEnterpriseAnnouncement = (enterpriseId: number, announcementId: number, token: string | null) =>
  del<null>(`/enterprises/${enterpriseId}/announcements/${announcementId}`, { authTokenSnapshot: token })
