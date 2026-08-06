import { get, post, put, del } from './http'
import httpClient from './http'
import type { Page } from '@/types/common'
import type {
  CodeFileOut,
  CodeFileDetailOut,
  CodeFileMetaOut,
  CodeFileUpdateIn,
  VersionOut,
  VersionDetailOut,
} from '@/types/project'

/**
 * 获取代码文件列表(分页)
 * @param params 查询参数
 * @returns 分页文件列表
 */
export function list(params: Record<string, unknown>): Promise<Page<CodeFileOut>> {
  return get<Page<CodeFileOut>>('/code-files', params)
}

/**
 * 上传代码文件
 * @param body FormData
 * @returns 上传结果
 */
export function upload(body: FormData): Promise<{ file_id: number; language: string; version_no: number }> {
  return post<{ file_id: number; language: string; version_no: number }>('/code-files/upload', body)
}

/**
 * 在线新增代码文件
 * @param body 文件参数
 * @returns 创建结果
 */
export function create(body: { project_id: number; file_name: string; file_path?: string; language?: string; content: string }): Promise<{ file_id: number; version_no: number }> {
  return post<{ file_id: number; version_no: number }>('/code-files', body)
}

/**
 * 获取代码文件详情(含内容)
 * @param fileId 文件ID
 * @returns 文件详情
 */
export function getDetail(fileId: number): Promise<CodeFileDetailOut> {
  return get<CodeFileDetailOut>(`/code-files/${fileId}`)
}

/**
 * 更新代码文件内容(生成新版本)
 * @param fileId 文件ID
 * @param body 更新参数
 * @returns 新版本号
 */
export function update(fileId: number, body: CodeFileUpdateIn): Promise<{ version_no: number }> {
  return put<{ version_no: number }>(`/code-files/${fileId}`, body)
}

/**
 * 重命名文件
 * @param fileId 文件ID
 * @param body 新文件名
 */
export function rename(fileId: number, body: { file_name: string; file_path?: string }): Promise<void> {
  return post<void>(`/code-files/${fileId}/rename`, body)
}

/**
 * 删除代码文件(软删除)
 * @param fileId 文件ID
 */
export function remove(fileId: number): Promise<void> {
  return del<void>(`/code-files/${fileId}`)
}

/**
 * 获取文件版本历史列表
 * @param fileId 文件ID
 * @param params 分页参数
 * @returns 版本列表
 */
export function listVersions(fileId: number, params?: Record<string, unknown>): Promise<Page<VersionOut>> {
  return get<Page<VersionOut>>(`/code-files/${fileId}/versions`, params)
}

/**
 * 获取指定版本内容
 * @param fileId 文件ID
 * @param versionNo 版本号
 * @returns 版本详情
 */
export function getVersion(fileId: number, versionNo: number): Promise<VersionDetailOut> {
  return get<VersionDetailOut>(`/code-files/${fileId}/versions/${versionNo}`)
}

/**
 * 回滚到指定版本
 * @param fileId 文件ID
 * @param versionNo 版本号
 * @returns 新版本号
 */
export function restoreVersion(fileId: number, versionNo: number): Promise<{ version_no: number }> {
  return post<{ version_no: number }>(`/code-files/${fileId}/versions/${versionNo}/restore`)
}

/**
 * 批量上传代码文件(文件夹上传)
 * @param projectId 项目ID
 * @param files 文件列表
 * @returns 上传结果
 */
export async function uploadFolder(
  projectId: number,
  files: File[],
): Promise<{
  success_count: number
  fail_count: number
  files: { file_name: string; file_id: number; language: string; version_no: number }[]
  errors: { file_name: string; error: string }[]
}> {
  const formData = new FormData()
  formData.append('project_id', String(projectId))
  for (const file of files) {
    formData.append('files', file, file.webkitRelativePath || file.name)
  }
  const r = await httpClient.post('/code-files/upload-folder', formData)
  return r.data.data
}

/**
 * 下载二进制文件原始字节
 * v2 新增:二进制文件(图片/可执行文件等)不通过编辑器展示 base64,
 * 前端通过此接口下载原文件。
 * 别名:downloadFile,与 T13 规范文档命名一致
 * @param fileId 文件ID
 * @returns Blob(可直接触发浏览器下载)
 */
export async function downloadBinary(fileId: number): Promise<Blob> {
  const r = await httpClient.get(`/code-files/${fileId}/download`, {
    responseType: 'blob',
  })
  return r.data
}

/**
 * 下载文件(与 downloadBinary 等价,提供语义化别名)
 * v3:对齐 T13 任务规范的 downloadFile(file_id) 命名
 * @param fileId 文件ID
 * @returns Blob(可直接触发浏览器下载)
 */
export async function downloadFile(fileId: number): Promise<Blob> {
  return downloadBinary(fileId)
}

/**
 * 获取文件元信息(不含内容)
 * v3 新增:二进制文件展示提示卡片时,通过此接口获取 MD5/类型等元数据
 * @param fileId 文件ID
 * @returns 文件元信息
 */
export function getFileMetadata(fileId: number): Promise<CodeFileMetaOut> {
  return get<CodeFileMetaOut>(`/code-files/${fileId}/meta`)
}
