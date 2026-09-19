import { beforeEach, describe, expect, it, vi } from 'vitest'

const httpApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('./http', () => ({
  get: httpApi.get,
  post: httpApi.post,
  put: vi.fn(),
  del: vi.fn(),
}))

import { getPost } from './forum'

const detail = {
  id: 7,
  user_id: 3,
  author_name: '测试用户',
  category: 'discussion',
  title: '只读详情',
  content: '正文',
  view_count: 4,
  reply_count: 0,
  is_pinned: false,
  create_time: '2026-09-19T00:00:00Z',
  update_time: '2026-09-19T00:00:00Z',
  replies: [],
}

describe('forum detail API', () => {
  beforeEach(() => {
    httpApi.get.mockReset().mockResolvedValue(detail)
    httpApi.post.mockReset()
  })

  it('先只读获取详情，再通过显式 POST 记录浏览', async () => {
    httpApi.post.mockResolvedValue({ ...detail, view_count: 5 })

    await expect(getPost(7)).resolves.toEqual({ ...detail, view_count: 5 })
    expect(httpApi.get).toHaveBeenCalledWith('/forum/posts/7')
    expect(httpApi.post).toHaveBeenCalledWith('/forum/posts/7/views')
  })

  it('浏览计数写入失败时仍返回已读取的详情', async () => {
    httpApi.post.mockRejectedValue(new Error('counter unavailable'))

    await expect(getPost(7)).resolves.toEqual(detail)
  })
})
