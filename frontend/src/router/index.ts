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
    redirect: '/dashboard',
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
        path: 'profile',
        name: 'ProfileCenter',
        component: () => import('@/views/profile/ProfileCenter.vue'),
        meta: { title: '个人中心' },
      },
      {
        path: 'profile/password',
        name: 'ChangePassword',
        component: () => import('@/views/profile/ChangePassword.vue'),
        meta: { title: '修改密码' },
      },
      {
        path: 'admin/users',
        name: 'UserManage',
        component: () => import('@/views/admin/UserManage.vue'),
        meta: { title: '用户管理', role: 'admin' },
      },
      {
        path: 'admin/ai-logs',
        name: 'AiLogList',
        component: () => import('@/views/admin/AiLogList.vue'),
        meta: { title: 'Agent 调用日志', role: 'admin' },
      },
      {
        path: 'admin/audit',
        name: 'SystemAudit',
        component: () => import('@/views/admin/SystemAudit.vue'),
        meta: { title: '系统操作审计', role: 'admin' },
      },
      {
        path: 'admin/evolution',
        name: 'EvolutionCenter',
        component: () => import('@/views/admin/EvolutionCenter.vue'),
        meta: { title: 'Agent 自进化', role: 'admin' },
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
