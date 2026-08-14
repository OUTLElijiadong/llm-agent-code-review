/**
 * 页面操作后的主动引导目录:用户/管理端路由 → 小菱弹出的下一步建议。
 * 与后端 page_guide_service 的路由清单口径一致,只做提示与唤起小菱,不代用户执行。
 */

export interface PageGuideTip {
  /** 路由前缀匹配;精确路由优先于前缀。 */
  route: string
  title: string
  hint: string
  /** 点击「让小菱继续引导」时预填给小菱的指令。 */
  prompt: string
}

export const USER_PAGE_TIPS: PageGuideTip[] = [
  { route: '/projects', title: '项目管理', hint: '可以导入 GitHub 仓库、上传源码并直接发起审查。', prompt: '我正在项目管理页，请带我完成一次项目导入并给出推荐审查方案。' },
  { route: '/code', title: '代码中心', hint: '浏览文件、在线编辑后记得复测;修复副本适合重新跑一次审计。', prompt: '我正在代码中心，请根据当前项目文件推荐下一步：审查/修复/复测。' },
  { route: '/reviews/start', title: '发起审查', hint: '选好项目与文件即可发起黑白盒或全链路审计。', prompt: '我准备发起代码审查，请帮我选择最适合的审查类型并说明原因。' },
  { route: '/reviews', title: '审查记录', hint: '查看任务进度、失败原因与重试入口。', prompt: '我在审查记录页，请帮我梳理当前任务并指出需要我处理的失败项。' },
  { route: '/issues', title: '问题追踪', hint: '按严重度闭环问题,优先处理 high/critical。', prompt: '我在问题追踪页，请按严重度帮我排一个处理顺序并给出首个问题的处置建议。' },
  { route: '/reports', title: '审查报告', hint: '报告可直接导出、分享或转成修复计划。', prompt: '我在审查报告页，请帮我解读最新报告并给出下一步行动。' },
  { route: '/security', title: '安全中心', hint: '查看安全审计清单与扫描任务,高风险项目优先。', prompt: '我在安全中心，请分析当前安全态势并建议优先处理哪些高风险项目。' },
  { route: '/sandboxes', title: '代码沙箱', hint: '可创建隔离测试环境并做白盒/黑盒验证。', prompt: '我在代码沙箱页，请帮我判断是否需要在沙箱复测并指导我操作。' },
  { route: '/agents', title: 'Agent 中心', hint: '查看 Agent 画像、团队协作与运行态势。', prompt: '我在 Agent 中心，请介绍当前可用的审查 Agent 并推荐合适的协作方式。' },
  { route: '/knowledge', title: '个人知识库', hint: '沉淀审查经验后,后续问答会自动复用。', prompt: '我在个人知识库，请帮我把最近的审查经验整理成一条知识笔记。' },
  { route: '/profile', title: '个人中心', hint: '维护个人资料、API 配置与密码安全。', prompt: '我在个人中心，请告诉我当前账号有哪些值得维护的安全项。' },
]

export const ADMIN_PAGE_TIPS: PageGuideTip[] = [
  { route: '/admin/overview', title: '总览大屏', hint: '系统状态、安全态势与 Agent 活跃一眼掌握。', prompt: '我在管理总览页，请做一次系统态势巡检并给出需要我处理的异常清单。' },
  { route: '/admin/approvals', title: '审批中心', hint: '待审批事项按风险排队,批准前先核对证据。', prompt: '我在审批中心，请帮我审查当前待审批事项并给出逐项处置建议。' },
  { route: '/admin/observability', title: '监控告警', hint: '查看告警、指标与 Agent 调度健康。', prompt: '我在监控告警页，请按当前指标和告警做一次运维体检并推荐处置动作。' },
  { route: '/admin/users', title: '用户管理', hint: '用户启停、改密等敏感操作需要审批链。', prompt: '我在用户管理页，请帮我核对用户异常并生成安全的处置步骤。' },
  { route: '/admin/audit', title: '系统操作审计', hint: '回溯高风险操作,确认是否越权。', prompt: '我在系统操作审计页，请帮我审查近期高风险操作并识别异常。' },
  { route: '/admin/mcp-workers', title: 'MCP 与沙箱节点', hint: '检查 Worker 健康、并发上限与租约。', prompt: '我在 MCP 与沙箱节点页，请检查 Worker 健康并给出运维建议。' },
  { route: '/admin/ai-logs', title: 'Agent 调用日志', hint: '定位失败调用与模型层异常。', prompt: '我在 Agent 调用日志页，请帮我定位近期失败调用并给出处置建议。' },
  { route: '/admin/jobs', title: '任务调度', hint: '管理 Agent 定时任务与执行记录。', prompt: '我在任务调度页，请帮我检查定时任务健康并推荐需要调整的项。' },
  { route: '/admin/rollback', title: '回滚中心', hint: '制品回滚属于高风险操作,先核对版本。', prompt: '我在回滚中心，请帮我核对版本差异并给出安全的回滚方案。' },
]

export function findPageGuideTip(surface: 'user' | 'admin', path: string): PageGuideTip | undefined {
  const tips = surface === 'admin' ? ADMIN_PAGE_TIPS : USER_PAGE_TIPS
  const exact = tips.find((item) => item.route === path)
  if (exact) return exact
  // 动态详情页(如 /reviews/123、/projects/45)按前缀兜底匹配,越具体越优先。
  return [...tips]
    .filter((item) => path === item.route || path.startsWith(`${item.route}/`))
    .sort((left, right) => right.route.length - left.route.length)[0]
}
