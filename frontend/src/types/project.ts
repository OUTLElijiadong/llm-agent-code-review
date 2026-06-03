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
  create_time: string
  update_time: string
}

export interface CodeFileDetailOut extends CodeFileOut {
  content: string
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
