/** Prism Agent 页面引导协议类型(与后端 page_guide_service 对齐) */

export interface AgentPageGuideItem {
  route: string
  label: string
  hint?: string
}

export interface AgentNavigateDirective {
  action: 'navigate'
  route: string
  label?: string
  hint?: string
}
