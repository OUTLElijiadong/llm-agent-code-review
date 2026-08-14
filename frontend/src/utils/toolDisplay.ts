/**
 * 工具名 → 通俗中文展示映射（纯展示层）。
 *
 * 设计目标:小菱聊天面板里的「工具调用链」不再把 `recall_knowledge`、
 * `get_projects` 这类代码级函数名直接外露给普通用户,而是翻译成
 * 「检索知识库」「查看项目」这样的人话;RAG 检索类工具单独标记,
 * 便于时间线/进度条给出「正在检索知识库…」的专门状态。
 *
 * 数据契约见 backend/app/agents/tool_contracts.py(固定工具清单),
 * 本文件只做 name → 展示信息 的映射,不改任何 SSE/时间线数据结构。
 */

export interface ToolDisplayInfo {
  /** 通俗动作名,如「检索知识库」 */
  label: string
  /** 进行时短语,如「小菱正在检索知识库…」 */
  running: string
  /** 分类,用于图标/配色分组 */
  group: 'knowledge' | 'project' | 'review' | 'code' | 'security' | 'sandbox' | 'prompt' | 'capability' | 'admin' | 'other'
  /** 是否 RAG/知识库检索类(检索时要显示「检索中」专属状态) */
  isRag: boolean
  /** 是否页面操作类(触发全屏彩框/虚拟鼠标) */
  isPageAction: boolean
}

interface ToolMeta {
  label: string
  running: string
  group: ToolDisplayInfo['group']
  isRag?: boolean
  isPageAction?: boolean
}

/** 精确工具名映射表(与 tool_contracts.py 对齐)。 */
const TOOL_META: Record<string, ToolMeta> = {
  // RAG / 知识库
  recall_knowledge: { label: '检索知识库', running: '小菱正在检索知识库…', group: 'knowledge', isRag: true },
  change_own_password: { label: '修改密码', running: '小菱正在帮你修改密码…', group: 'admin', isPageAction: true },
  save_knowledge_note: { label: '记下经验', running: '小菱正在记下经验…', group: 'knowledge', isRag: true },

  // 项目
  list_projects: { label: '查看项目列表', running: '小菱正在查看项目列表…', group: 'project' },
  get_project_detail: { label: '查看项目详情', running: '小菱正在查看项目详情…', group: 'project' },
  create_project: { label: '创建项目', running: '小菱正在创建项目…', group: 'project', isPageAction: true },
  update_project: { label: '更新项目', running: '小菱正在更新项目…', group: 'project', isPageAction: true },
  delete_project: { label: '删除项目', running: '小菱正在删除项目…', group: 'project', isPageAction: true },

  // 审查任务
  start_review: { label: '发起代码审查', running: '小菱正在发起代码审查…', group: 'review', isPageAction: true },
  list_review_tasks: { label: '查看审查记录', running: '小菱正在查看审查记录…', group: 'review' },
  list_review_issues: { label: '查看审查问题', running: '小菱正在查看审查问题…', group: 'review' },
  list_reports: { label: '查看审查报告', running: '小菱正在查看审查报告…', group: 'review' },

  // 代码分析
  list_code_files: { label: '查看代码文件', running: '小菱正在查看代码文件…', group: 'code' },
  detect_language: { label: '识别编程语言', running: '小菱正在识别编程语言…', group: 'code' },
  analyze_project: { label: '分析项目代码', running: '小菱正在分析项目代码…', group: 'code' },
  review_code: { label: '审查代码', running: '小菱正在审查代码…', group: 'code' },

  // AI 修复提示
  generate_ai_prompt_for_issue: { label: '生成修复提示', running: '小菱正在生成修复提示…', group: 'prompt' },
  generate_ai_prompt_for_task: { label: '生成修复提示', running: '小菱正在生成修复提示…', group: 'prompt' },
  generate_ai_prompt_for_project: { label: '生成修复提示', running: '小菱正在生成修复提示…', group: 'prompt' },

  // 安全审计
  audit_security_for_file: { label: '审计文件安全', running: '小菱正在审计文件安全…', group: 'security' },
  audit_security_for_task: { label: '审计任务安全', running: '小菱正在审计任务安全…', group: 'security' },
  audit_security_for_project: { label: '审计项目安全', running: '小菱正在审计项目安全…', group: 'security' },

  // 测试 / 沙箱
  run_project_tests: { label: '运行项目测试', running: '小菱正在运行项目测试…', group: 'sandbox', isPageAction: true },
  deploy_project_sandbox: { label: '部署沙箱', running: '小菱正在部署沙箱…', group: 'sandbox', isPageAction: true },
  close_sandbox: { label: '关闭沙箱', running: '小菱正在关闭沙箱…', group: 'sandbox', isPageAction: true },
  extend_sandbox: { label: '续期沙箱', running: '小菱正在续期沙箱…', group: 'sandbox', isPageAction: true },

  // Agent 元数据 / 工坊
  list_agents: { label: '查看 Agent 列表', running: '小菱正在查看 Agent…', group: 'other' },
  list_rules: { label: '查看审查规则', running: '小菱正在查看审查规则…', group: 'review' },
  dashboard_summary: { label: '查看工作台概览', running: '小菱正在查看工作台…', group: 'other' },
  trigger_evolution: { label: '触发自进化', running: '小菱正在触发自进化…', group: 'admin', isPageAction: true },
  list_agent_skills: { label: '查看 Agent 技能', running: '小菱正在查看技能…', group: 'other' },
  search_published_agents: { label: '搜索已发布 Agent', running: '小菱正在搜索 Agent…', group: 'other' },
  invoke_published_agent: { label: '调用已发布 Agent', running: '小菱正在调用 Agent…', group: 'other' },

  // 页面能力(帮我操作)
  user_execute_capability: { label: '执行页面操作', running: '小菱正在帮你操作页面…', group: 'capability', isPageAction: true },
  admin_execute_capability: { label: '执行管理操作', running: '小菱正在执行管理操作…', group: 'capability', isPageAction: true },

  // 多智能体编排(子 Agent 团队)
  create_agent_team: { label: '创建子Agent团队', running: '小菱正在组建子Agent团队…', group: 'capability', isPageAction: true },
  get_agent_team: { label: '查看团队进度', running: '小菱正在查看团队进度…', group: 'capability' },
  list_agent_teams: { label: '查看团队列表', running: '小菱正在查看团队列表…', group: 'capability' },
  retry_agent_team: { label: '重试失败任务', running: '小菱正在重试失败任务…', group: 'capability', isPageAction: true },
  cancel_agent_team: { label: '取消团队', running: '小菱正在取消团队…', group: 'capability', isPageAction: true },
  archive_agent_team: { label: '归档团队', running: '小菱正在归档团队…', group: 'capability' },
  send_agent_message: { label: '给子Agent发消息', running: '小菱正在给子Agent派活…', group: 'capability' },
  receive_agent_message: { label: '接收子Agent消息', running: '小菱正在接收子Agent反馈…', group: 'capability' },
  dispatch_agent_task: { label: '派发子任务', running: '小菱正在派发子任务…', group: 'capability' },
}

/** 前缀兜底规则(命中第一个匹配项)。 */
const PREFIX_META: Array<{ prefix: string; meta: ToolMeta }> = [
  { prefix: 'admin_list', meta: { label: '查看管理数据', running: '小菱正在查看管理数据…', group: 'admin' } },
  { prefix: 'admin_delete', meta: { label: '删除管理数据', running: '小菱正在删除管理数据…', group: 'admin', isPageAction: true } },
  { prefix: 'admin_set', meta: { label: '修改配置', running: '小菱正在修改配置…', group: 'admin', isPageAction: true } },
  { prefix: 'admin_toggle', meta: { label: '切换开关', running: '小菱正在切换开关…', group: 'admin', isPageAction: true } },
  { prefix: 'admin_decide', meta: { label: '处理审批', running: '小菱正在处理审批…', group: 'admin', isPageAction: true } },
  { prefix: 'admin_execute', meta: { label: '执行管理操作', running: '小菱正在执行管理操作…', group: 'capability', isPageAction: true } },
  { prefix: 'admin_', meta: { label: '管理操作', running: '小菱正在执行管理操作…', group: 'admin' } },
  { prefix: 'ask_user', meta: { label: '向你确认', running: '小菱想问你一个问题…', group: 'other' } },
]

/** 把 snake_case 工具名转成空格分隔的可读短语(最终兜底,不做标题)。 */
function humanizeSnake(name: string): string {
  return name.replace(/[_-]+/g, ' ').trim()
}

/**
 * 取工具的通俗展示信息。
 * @param name 原始工具名(可能为空/未知)
 * @returns 展示信息;未知工具给出可读短语兜底
 */
export function toolDisplayInfo(name: string | undefined | null): ToolDisplayInfo {
  const key = (name ?? '').trim()
  if (key) {
    const exact = TOOL_META[key]
    if (exact) {
      return { label: exact.label, running: exact.running, group: exact.group, isRag: exact.isRag === true, isPageAction: exact.isPageAction === true }
    }
    const prefixed = PREFIX_META.find((rule) => key.startsWith(rule.prefix))
    if (prefixed) {
      const meta = prefixed.meta
      return { label: meta.label, running: meta.running, group: meta.group, isRag: meta.isRag === true, isPageAction: meta.isPageAction === true }
    }
    const readable = humanizeSnake(key)
    return { label: readable, running: `小菱正在执行 ${readable}…`, group: 'other', isRag: false, isPageAction: false }
  }
  return { label: '处理中', running: '小菱正在处理…', group: 'other', isRag: false, isPageAction: false }
}

/**
 * 取工具进行时的通俗短语(进度条/时间线高亮用)。
 * @param name 原始工具名
 */
export function toolRunningPhrase(name: string | undefined | null): string {
  return toolDisplayInfo(name).running
}

/**
 * 判断工具是否 RAG/知识库检索类(检索时显示「检索中」)。
 * @param name 原始工具名
 */
export function isKnowledgeTool(name: string | undefined | null): boolean {
  return toolDisplayInfo(name).isRag
}

/**
 * 判断工具是否页面操作类(触发彩框/虚拟鼠标)。
 * @param name 原始工具名
 */
export function isPageActionTool(name: string | undefined | null): boolean {
  return toolDisplayInfo(name).isPageAction
}
