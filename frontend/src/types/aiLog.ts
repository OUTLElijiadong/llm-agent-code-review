export interface AiLogOut {
  id: number
  task_id?: number
  task_name?: string
  project_id?: number
  project_name?: string
  user_id?: number
  user_name?: string
  file_id?: number
  file_name?: string
  chunk_index?: number
  model_name: string
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  duration_ms?: number
  status: string
  create_time: string
}

export interface AiLogDetailOut {
  id: number
  task_id?: number
  task_name?: string
  project_id?: number
  project_name?: string
  user_id?: number
  user_name?: string
  file_id?: number
  file_name?: string
  chunk_index?: number
  model_name: string
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  prompt?: string
  response?: string
  status: string
  error_message?: string
  duration_ms?: number
  create_time: string
}
