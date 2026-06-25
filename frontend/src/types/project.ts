export interface ProjectOut {
  id: number
  project_name: string
  description?: string
  language?: string
  status: string
  file_count: number
  last_review_at?: string
  /** v2.0: 最近一次成功审查的真实评分,无审查记录时为 null */
  score?: number | null
  create_time: string
}

export interface ProjectDetailOut {
  id: number
  project_name: string
  description?: string
  language?: string
  status: string
  file_count: number
  create_time: string
  update_time: string
  recent_tasks: { id: number; score: number; total_issues: number; status: string; create_time: string }[]
}

export interface CodeFileOut {
  id: number
  project_id: number
  file_name: string
  file_path?: string
  language: string
  size_bytes: number
  line_count: number
  version_no: number
  /** v2: 是否二进制文件(0否 1是),二进制文件不展示编辑器,改用下载接口 */
  is_binary: number
  /** v3: 文件 MIME 类型(如 image/png、application/zip),用于前端图标识别 */
  mime_type?: string
  /** v3: 原始字节数(对二进制文件等于 original_blob 长度,文本文件等于 content 编码长度) */
  raw_size?: number
  create_time: string
  update_time: string
}

export interface CodeFileDetailOut extends CodeFileOut {
  content: string
  /** v3: 文件 MD5 摘要(后端 T06 入库时计算) */
  md5_hash?: string
  /** v3: 文件 SHA-256 摘要 */
  sha256_hash?: string
}

/**
 * 文件元信息(用于二进制文件展示)
 * 不含内容,仅元数据
 */
export interface CodeFileMetaOut {
  id: number
  file_name: string
  file_path?: string
  language: string
  size_bytes: number
  raw_size?: number
  line_count: number
  version_no: number
  is_binary: number
  mime_type?: string
  md5_hash?: string
  sha256_hash?: string
  create_time: string
  update_time: string
}

export interface VersionOut {
  version_no: number
  change_desc?: string
  operator_id?: number
  create_time: string
}

export interface VersionDetailOut {
  file_id: number
  version_no: number
  content: string
  change_desc?: string
  create_time: string
}

export interface ProjectCreateIn {
  project_name: string
  description?: string
  language?: string
}

export interface ProjectUpdateIn {
  project_name?: string
  description?: string
  language?: string
  status?: string
}

export interface CodeFileCreateIn {
  file_name: string
  file_path?: string
  content: string
  language?: string
}

export interface CodeFileUpdateIn {
  content: string
  change_desc?: string
}

export interface CodeFileRenameIn {
  file_name: string
}

export interface CodeFileUploadOut {
  file_name: string
  size_bytes: number
  language: string
}
