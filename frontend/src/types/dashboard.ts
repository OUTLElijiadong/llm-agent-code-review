export interface RecentTaskOut {
  id: number
  task_name: string
  project_id: number
  project_name: string
  status: string
  score: number | null
  review_type?: string | null
  create_time: string | null
}

export interface SummaryOut {
  project_count: number
  file_count: number
  archive_file_count?: number
  review_count: number
  total_issues: number
  severe_issues: number
  avg_score: number
  code_review_count?: number
  recent_tasks: RecentTaskOut[]
}

export interface RiskItem {
  severity: string
  count: number
}

export interface IssueTypeItem {
  issue_type: string
  count: number
}

export interface ScoreTrendItem {
  task_id: number
  score: number
  create_time: string
}

export interface FrequencyItem {
  date: string
  count: number
}

export interface RunningReviewItem {
  id: number
  task_name: string
  project_id: number
  project_name: string
  review_type: string
  status: string
  processed_files: number
  total_files: number
  create_time?: string | null
}

export interface RunningAgentItem {
  run_id: string
  surface: string
  session_key: string
  status: string
  update_time?: string | null
}

export interface RunningOut {
  reviews: RunningReviewItem[]
  agents: RunningAgentItem[]
}
