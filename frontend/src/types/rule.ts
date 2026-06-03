export interface RuleOut {
  id: number
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  language: string
  severity: string
  enabled: number
  is_builtin: number
  sort_order: number
  create_time?: string
  update_time?: string
}

export interface RuleIn {
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  language?: string
  severity?: string
}

export interface RuleUpdateIn {
  rule_name?: string
  rule_type?: string
  rule_content?: string
  language?: string
  severity?: string
}
