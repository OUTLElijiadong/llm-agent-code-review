export interface ReportListItem {
  task_id: number
  task_name?: string
  project_name: string
  total_issues: number
  score: number
  create_time: string
}

export interface ReportDetailOut {
  project: Record<string, unknown>
  task: Record<string, unknown>
  stats: Record<string, unknown>
  summary?: string
  files: Record<string, unknown>[]
  rules_snapshot: Record<string, unknown>[]
}
