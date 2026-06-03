export interface AuditLogOut {
  id: number
  actor_id?: number | null
  actor_name?: string | null
  action: string
  target_type?: string | null
  target_id?: string | null
  detail?: string | null
  status: string
  ip?: string | null
  create_time: string
}

export interface AuditQuery {
  action?: string
  keyword?: string
  actor_id?: number
  start?: string
  end?: string
  page?: number
  page_size?: number
}
