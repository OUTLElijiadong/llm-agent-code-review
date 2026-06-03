export interface RuleOut {
  id: number
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  enabled: number
  is_builtin: number
  sort_order: number
}

export interface IssueOut {
  id: number
  task_id: number
  file_id?: number
  file_name?: string
  line_number?: number
  end_line?: number
  issue_type: string
  severity: string
  title?: string
  description: string
  suggestion?: string
  fixed_code?: string
  status: string
  create_time: string
}

export interface IssueListItemOut extends IssueOut {
  project_id: number
  project_name: string
  task_name?: string
}

export interface IssueQuery {
  project_id?: number
  task_id?: number
  severity?: string
  issue_type?: string
  status?: string
  keyword?: string
  page?: number
  page_size?: number
}

export interface TaskOut {
  id: number
  task_name?: string
  project_id: number
  project_name: string
  review_type: string
  status: string
  total_files: number
  total_issues: number
  severe_issues: number
  high_issues: number
  medium_issues: number
  low_issues: number
  score: number
  duration_ms: number
  create_time: string
}

export interface TaskFileOut {
  file_id: number
  project_id: number
  file_name: string
  file_path?: string
  language: string
  line_count: number
  version_no: number
}

export interface TaskDetailOut {
  id: number
  task_name?: string
  project_id: number
  project_name: string
  review_type: string
  status: string
  total_files: number
  processed_files: number
  total_issues: number
  severe_issues: number
  high_issues: number
  medium_issues: number
  low_issues: number
  score: number
  summary?: string
  model_name?: string
  duration_ms: number
  start_time?: string
  end_time?: string
  create_time: string
  files: TaskFileOut[]
}

export interface ReviewStartIn {
  project_id: number
  file_ids: number[]
  review_type?: string
  task_name?: string
}

export interface RuleCreateIn {
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  enabled?: number
  sort_order?: number
}

export interface RuleUpdateIn {
  rule_name?: string
  rule_type?: string
  rule_content?: string
  enabled?: number
  sort_order?: number
}

export interface IssueUpdateStatusIn {
  status: string
}

export interface IssueBatchUpdateStatusIn {
  ids: number[]
  status: string
}
