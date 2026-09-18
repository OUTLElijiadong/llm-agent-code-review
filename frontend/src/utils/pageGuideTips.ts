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
  { route: '/security', title: '安全与审查规则', hint: '统一查看安全态势并维护审查规则,高风险项目优先。', prompt: '我在安全与审查规则页，请分析当前安全态势并建议优先处理哪些高风险项目。' },
  { route: '/sandboxes', title: '代码沙箱', hint: '可创建隔离测试环境并做白盒/黑盒验证。', prompt: '我在代码沙箱页，请帮我判断是否需要在沙箱复测并指导我操作。' },
  { route: '/agents', title: 'Agent 工作台', hint: '统一查看 Agent、创建草稿并进行测试。', prompt: '我在 Agent 工作台，请介绍当前可用的审查 Agent 并推荐合适的协作方式。' },
  { route: '/knowledge', title: '个人知识库', hint: '沉淀审查经验后,后续问答会自动复用。', prompt: '我在个人知识库，请帮我把最近的审查经验整理成一条知识笔记。' },
  { route: '/profile', title: '个人中心', hint: '维护个人资料、API 配置与密码安全。', prompt: '我在个人中心，请告诉我当前账号有哪些值得维护的安全项。' },
]

export const ADMIN_PAGE_TIPS: PageGuideTip[] = [
  { route: '/admin/governance', title: 'Agent 治理中心', hint: '集中管理 Agent、知识、技能与发布审批。', prompt: '我在 Agent 治理中心，请按风险检查待审批事项和运行中的 Agent。' },
  { route: '/admin/operations', title: '运行与审计中心', hint: '集中查看运行总览、监控、调用日志和系统审计。', prompt: '我在运行与审计中心，请按当前指标和告警做一次运维体检。' },
  { route: '/admin/access', title: '用户与权限中心', hint: '集中管理用户、角色和权限点，避免重复分配入口。', prompt: '我在用户与权限中心，请帮我核对用户、角色和权限边界。' },
  { route: '/admin/platform', title: '平台配置中心', hint: '集中维护模型、RAG、内测码、报告模板和节点配置。', prompt: '我在平台配置中心，请检查当前模型和平台基础配置是否完整。' },
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
