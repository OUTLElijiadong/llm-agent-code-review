import { beforeEach, describe, expect, it, vi } from 'vitest'

const httpApi = vi.hoisted(() => ({
  post: vi.fn(),
  get: vi.fn(),
}))

vi.mock('./http', () => ({
  default: { post: httpApi.post },
  get: httpApi.get,
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  download: vi.fn(),
}))

import { cancelRemoteProjectImport, getRemoteProjectImport, queueRemoteProjectImport } from './project'

describe('project remote import API', () => {
  beforeEach(() => {
    httpApi.post.mockReset()
    httpApi.get.mockReset()
  })

  it('使用 Idempotency-Key 创建持久化远程导入任务', async () => {
    const task = { task_id: 'task-1', status: 'queued' }
    httpApi.post.mockResolvedValue({ data: { data: task } })
    const payload = {
      url: 'https://example.com/project.zip',
      project_name: '测试项目',
      description: '测试',
      audit_mode: false,
    }

    await expect(queueRemoteProjectImport(payload, 'prism-remote-import-key'))
      .resolves.toEqual(task)

    expect(httpApi.post).toHaveBeenCalledWith(
      '/projects/remote-imports',
      payload,
      { headers: { 'Idempotency-Key': 'prism-remote-import-key' } },
    )
  })

  it('按 URL 编码的任务 ID 查询状态', async () => {
    const task = { task_id: 'task/with-space', status: 'running' }
    httpApi.get.mockResolvedValue(task)

    await expect(getRemoteProjectImport('task/with-space')).resolves.toEqual(task)
    expect(httpApi.get).toHaveBeenCalledWith('/projects/remote-imports/task%2Fwith-space')
  })

  it('携带取消原因停止远程导入任务', async () => {
    const task = { task_id: 'task/with-space', status: 'cancelled' }
    httpApi.post.mockResolvedValue({ data: { data: task } })

    await expect(cancelRemoteProjectImport('task/with-space', '用户取消'))
      .resolves.toEqual(task)

    expect(httpApi.post).toHaveBeenCalledWith(
      '/projects/remote-imports/task%2Fwith-space/cancel',
      { reason: '用户取消' },
    )
  })
})
