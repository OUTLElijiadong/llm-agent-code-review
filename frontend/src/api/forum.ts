import { get, post, put, del } from './http'
import type { Page } from '@/types/common'

export interface ForumPost {
  id: number
  user_id: number
  author_name: string
  category: string
  title: string
  view_count: number
  reply_count: number
  is_pinned: boolean
  create_time: string
  update_time: string
}

export interface ForumReply {
  id: number
  post_id: number
  user_id: number
  author_name: string
  content: string
  create_time: string
}

export interface ForumPostDetail extends ForumPost {
  content: string
  replies: ForumReply[]
}

export interface AssistResult {
  suggestion: string
  references: { title: string; source_type: string; score: number }[]
}

/** 帖子列表 */
export function getPosts(params?: Record<string, unknown>): Promise<Page<ForumPost>> {
  return get<Page<ForumPost>>('/forum/posts', params)
}

/** 帖子详情 + 回复 */
export function getPost(id: number): Promise<ForumPostDetail> {
  return get<ForumPostDetail>(`/forum/posts/${id}`)
}

/** 发帖 */
export function createPost(data: {
  title: string; content: string; category?: string
}): Promise<{ id: number }> {
  return post<{ id: number }>('/forum/posts', data)
}

/** 编辑帖子 */
export function updatePost(id: number, data: {
  title?: string; content?: string; category?: string
}): Promise<ForumPostDetail> {
  return put<ForumPostDetail>(`/forum/posts/${id}`, data)
}

/** 删除帖子 */
export function deletePost(id: number): Promise<void> {
  return del<void>(`/forum/posts/${id}`)
}

/** 置顶/取消置顶 (管理员) */
export function pinPost(id: number, pinned: boolean): Promise<ForumPost> {
  return put<ForumPost>(`/forum/posts/${id}/pin`, { pinned })
}

/** 回帖 */
export function createReply(postId: number, content: string): Promise<{ id: number }> {
  return post<{ id: number }>(`/forum/posts/${postId}/replies`, { content })
}

/** 删除回复 */
export function deleteReply(replyId: number): Promise<void> {
  return del<void>(`/forum/replies/${replyId}`)
}

/** 发帖助手 (RAG) */
export function assistDraft(data: { title?: string; draft: string }): Promise<AssistResult> {
  return post<AssistResult>('/forum/assist', data)
}
