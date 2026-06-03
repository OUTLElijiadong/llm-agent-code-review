export interface AiPromptToolOut {
  value: string
  label: string
}

export interface AiPromptItemOut {
  issue_id: number
  kind?: 'issue' | 'aggregate'
  title: string
  target_tool: string
  target_label: string
  file_path: string
  lines: string
  language: string
  severity: string
  issue_type: string
  prompt_text: string
  code_context: string
  tokens: Record<string, number>
}

export interface AiPromptBundleOut {
  prompts: AiPromptItemOut[]
  aggregates?: AiPromptItemOut[]
  summary: string
}

export interface AiPromptIssueIn {
  issue_id: number
  target_tool: string
  use_llm?: boolean
}

export interface AiPromptTaskIn {
  task_id: number
  target_tool: string
  severity?: string[]
  use_llm?: boolean
}

export interface AiPromptProjectIn {
  project_id: number
  target_tool: string
  top_n?: number
  use_llm?: boolean
}
