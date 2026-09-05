import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({
  client: { post: vi.fn() },
  download: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}))

vi.mock('./http', () => ({
  default: http.client,
  download: http.download,
  get: http.get,
  post: http.post,
  put: http.put,
  del: http.del,
}))

import { exportReport, generateReport } from './report'

function domainErrorBlob() {
  return new Blob([JSON.stringify({
    code: 40941,
    message: '领域报告不支持 PDF',
    next_action: '请导出真实领域 JSON',
    retryable: false,
  })], { type: 'application/json' })
}

beforeEach(() => vi.resetAllMocks())

describe('报告导出错误边界', () => {
  it('统一导出接口不把错误 JSON Blob 当成功文件返回', async () => {
    http.download.mockResolvedValue(domainErrorBlob())
    await expect(exportReport(42, 'pdf')).rejects.toMatchObject({
      code: 40941,
      message: '领域报告不支持 PDF',
      next_action: '请导出真实领域 JSON',
    })
  })

  it('生成接口不把40941错误 JSON当作PDF文件返回', async () => {
    http.client.post.mockResolvedValue({ data: domainErrorBlob() })
    await expect(generateReport(42, 'pdf')).rejects.toMatchObject({ code: 40941 })
  })

  it('有效领域 JSON Blob保持可下载且不经过错误解析', async () => {
    const payload = { document_type: 'domain_report', schema_version: 'domain-report-v1', native_exports: [] }
    const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' })
    http.download.mockResolvedValue(blob)
    await expect(exportReport(42, 'json')).resolves.toBe(blob)
    expect(http.download).toHaveBeenCalledWith('/reports/tasks/42/export', {
      format: 'json', template_type: 'detailed',
    })
  })
})
