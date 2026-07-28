import { createRouter, createWebHistory, RouteRecordRaw } from 'vue-router'
import { setupGuards } from './guards'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/auth/Login.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('@/views/auth/Register.vue'),
    meta: { title: '注册', public: true },
  },
  {
    path: '/',
    component: () => import('@/components/layout/AppLayout.vue'),
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/dashboard/Dashboard.vue'),
        meta: { title: '工作台' },
      },
      {
        path: 'projects',
        name: 'ProjectList',
        component: () => import('@/views/project/ProjectList.vue'),
        meta: { title: '项目管理' },
      },
      {
        path: 'projects/:id',
        name: 'ProjectDetail',
        component: () => import('@/views/project/ProjectDetail.vue'),
        meta: { title: '项目详情' },
      },
      {
        path: 'reviews',
        name: 'ReviewTaskList',
        component: () => import('@/views/review/ReviewTaskList.vue'),
        meta: { title: '审查记录' },
      },
      {
        path: 'reviews/:id',
        name: 'ReviewTaskDetail',
        component: () => import('@/views/review/ReviewTaskDetail.vue'),
        meta: { title: '审查详情' },
      },
      {
        path: 'reviews/start',
        name: 'ReviewStart',
        component: () => import('@/views/review/ReviewStart.vue'),
        meta: { title: '发起审查' },
      },
      {
        path: 'rules',
        name: 'RuleConfig',
        component: () => import('@/views/rule/RuleConfig.vue'),
        meta: { title: '审查规则' },
      },
      {
        path: 'security',
        name: 'SecurityCenter',
        component: () => import('@/views/security/SecurityCenter.vue'),
        meta: { title: '安全中心' },
      },
      {
        path: 'reports',
        name: 'ReportList',
        component: () => import('@/views/report/ReportList.vue'),
        meta: { title: '审查报告' },
      },
      {
        path: 'reports/:id',
        name: 'ReportDetail',
        component: () => import('@/views/report/ReportDetail.vue'),
        meta: { title: '报告详情' },
      },
      {
        path: 'report/templates',
        name: 'ReportTemplateManage',
        component: () => import('@/views/report/ReportTemplateManage.vue'),
        meta: { title: '报告模板管理', permissions: ['report:template_manage'] },
      },
      {
        path: 'code/:projectId',
        name: 'CodeFileList',
        component: () => import('@/views/code/CodeFileList.vue'),
        meta: { title: '代码文件' },
        props: (route) => ({ projectId: Number(route.params.projectId) }),
      },
      {
        path: 'code/:projectId/file/:fileId',
        name: 'CodeEditor',
        component: () => import('@/views/code/CodeEditor.vue'),
        meta: { title: '编辑器' },
      },
      {
        path: 'code/:projectId/file/:fileId/versions',
        name: 'VersionHistory',
        component: () => import('@/views/code/VersionHistory.vue'),
        meta: { title: '版本历史' },
      },
      {
        path: 'code',
        name: 'CodeHub',
        component: () => import('@/views/code/CodeHub.vue'),
        meta: { title: '代码中心' },
      },
      {
        path: 'issues',
        name: 'IssueHub',
        component: () => import('@/views/issue/IssueHub.vue'),
        meta: { title: '问题追踪' },
      },
      {
        path: 'agents',
        name: 'AgentCenter',
        component: () => import('@/views/agent/AgentCenter.vue'),
        meta: { title: 'Agent 中心' },
      },
      {
        path: 'forum',
        name: 'ForumList',
        component: () => import('@/views/forum/ForumList.vue'),
        meta: { title: '开发者论坛' },
      },
      {
        path: 'forum/new',
        name: 'ForumPostCreate',
        component: () => import('@/views/forum/ForumPostEdit.vue'),
        meta: { title: '发布新帖' },
      },
      {
        path: 'forum/:id',
        name: 'ForumPostDetail',
        component: () => import('@/views/forum/ForumPostDetail.vue'),
        meta: { title: '帖子详情' },
      },
      {
        path: 'forum/:id/edit',
        name: 'ForumPostEdit',
        component: () => import('@/views/forum/ForumPostEdit.vue'),
        meta: { title: '编辑帖子' },
      },
      {
        path: 'knowledge',
        name: 'KnowledgeBase',
        component: () => import('@/views/knowledge/KnowledgeBase.vue'),
        meta: { title: '个人知识库' },
      },
      {
        path: 'support/maintenance',
        name: 'MaintenanceCenter',
        component: () => import('@/views/support/MaintenanceCenter.vue'),
        meta: { title: '申请维修' },
      },
      {
        path: 'support/feedback',
        name: 'FeedbackCenter',
        component: () => import('@/views/support/FeedbackCenter.vue'),
        meta: { title: '意见反馈' },
      },
      {
        path: 'profile',
        name: 'ProfileCenter',
        component: () => import('@/views/profile/ProfileCenter.vue'),
        meta: { title: '个人中心' },
      },
      {
        path: 'profile/personalization',
        name: 'PersonalizationCenter',
        component: () => import('@/views/profile/PersonalizationCenter.vue'),
        meta: { title: '个性化画像' },
      },
      {
        path: 'profile/password',
        name: 'ChangePassword',
        component: () => import('@/views/profile/ChangePassword.vue'),
        meta: { title: '修改密码' },
      },
      {
        path: 'profile/api-config',
        name: 'ApiConfig',
        component: () => import('@/views/profile/ApiConfig.vue'),
        meta: { title: 'API 配置' },
      },
    ],
  },
  {
    path: '/admin',
    component: () => import('@/components/admin/AdminLayout.vue'),
    meta: { role: 'admin', roles: ['admin'] },
    children: [
      {
        path: '',
        redirect: '/admin/overview',
      },
      {
        path: 'overview',
        name: 'AdminOverview',
        component: () => import('@/views/admin/AdminOverview.vue'),
        meta: { title: '总览大屏', role: 'admin', roles: ['admin'] },
      },
      {
        path: 'agents',
        name: 'AgentGovernance',
        component: () => import('@/views/admin/AgentGovernance.vue'),
        meta: { title: 'Agent 管理', role: 'admin' },
      },
      {
        path: 'approvals',
        name: 'ApprovalCenter',
        component: () => import('@/views/admin/ApprovalCenter.vue'),
        meta: { title: '审批中心', role: 'admin' },
      },
      {
        path: 'policies',
        name: 'PolicyCenter',
        component: () => import('@/views/admin/PolicyCenter.vue'),
        meta: { title: '策略中心', role: 'admin' },
      },
      {
        path: 'tools',
        name: 'ToolGovernance',
        component: () => import('@/views/admin/ToolGovernance.vue'),
        meta: { title: '工具权限', role: 'admin' },
      },
      {
        path: 'knowledge',
        name: 'KnowledgeGovernance',
        component: () => import('@/views/admin/KnowledgeGovernance.vue'),
        meta: { title: '知识与记忆', role: 'admin' },
      },
      {
        path: 'jobs',
        name: 'JobCenter',
        component: () => import('@/views/admin/JobCenter.vue'),
        meta: { title: '任务调度', role: 'admin' },
      },
      {
        path: 'observability',
        name: 'ObservabilityCenter',
        component: () => import('@/views/admin/ObservabilityCenter.vue'),
        meta: { title: '监控告警', role: 'admin' },
      },
      {
        path: 'rewards',
        name: 'RewardCenter',
        component: () => import('@/views/admin/RewardCenter.vue'),
        meta: { title: '奖惩趋势', role: 'admin' },
      },
      {
        path: 'rollback',
        name: 'RollbackCenter',
        component: () => import('@/views/admin/RollbackCenter.vue'),
        meta: { title: '回滚中心', role: 'admin' },
      },
      {
        path: 'users',
        name: 'UserManage',
        component: () => import('@/views/admin/UserManage.vue'),
        meta: { title: '用户管理', role: 'admin', roles: ['admin'], permissions: ['user:view'] },
      },
      {
        path: 'rbac/roles',
        name: 'RoleManage',
        component: () => import('@/views/admin/RoleManage.vue'),
        meta: { title: '角色管理', role: 'admin', roles: ['admin'], permissions: ['role:manage'] },
      },
      {
        path: 'rbac/permissions',
        name: 'PermissionList',
        component: () => import('@/views/admin/PermissionList.vue'),
        meta: { title: '权限点列表', role: 'admin', roles: ['admin'], permissions: ['role:manage'] },
      },
      {
        path: 'rbac/users',
        name: 'UserRoleAssign',
        component: () => import('@/views/admin/UserRoleAssign.vue'),
        meta: { title: '用户角色分配', role: 'admin', roles: ['admin'], permissions: ['user:view'] },
      },
      {
        path: 'ai-logs',
        name: 'AiLogList',
        component: () => import('@/views/admin/AiLogList.vue'),
        meta: { title: 'Agent 调用日志', role: 'admin' },
      },
      {
        path: 'audit',
        name: 'SystemAudit',
        component: () => import('@/views/admin/SystemAudit.vue'),
        meta: { title: '系统操作审计', role: 'admin', permissions: ['audit:view'] },
      },
      {
        path: 'evolution',
        name: 'EvolutionCenter',
        component: () => import('@/views/admin/EvolutionCenter.vue'),
        meta: { title: 'Agent 自进化', role: 'admin' },
      },
      {
        path: 'skills',
        name: 'SkillManager',
        component: () => import('@/views/admin/SkillManager.vue'),
        meta: { title: 'Skill 管理', role: 'admin' },
      },
      {
        path: 'embedding',
        name: 'EmbeddingConfig',
        component: () => import('@/views/admin/EmbeddingConfig.vue'),
        meta: { title: 'RAG 嵌入配置', role: 'admin' },
      },
      {
        path: 'llm',
        name: 'LlmConfig',
        component: () => import('@/views/admin/LlmConfig.vue'),
        meta: { title: '大模型配置', role: 'admin' },
      },
    ],
  },
  {
    path: '/403',
    name: 'Forbidden',
    component: () => import('@/views/error/Forbidden.vue'),
    meta: { title: '无权限', public: true },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/error/NotFound.vue'),
    meta: { title: '页面不存在', public: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

setupGuards(router)

export default router
