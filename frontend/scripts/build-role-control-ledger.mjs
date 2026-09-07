// 生成完整来源台账；源码、测试替身与旧版点击都不能升级为新版生产通过。
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { parse as parseSfc } from '@vue/compiler-sfc'

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const root = path.dirname(frontend)
const evidence = path.join(root, 'docs/完整待办与漏洞知识更新20260907/证据')
const inventoryPath = process.argv[2] ? path.resolve(process.argv[2]) : path.join(evidence, '全部按钮与交互源码清单-最终.json')
const outputPrefix = process.argv[3] ? path.resolve(process.argv[3]) : path.join(evidence, '全部控件逐角色验收')
const ledgerPath = `${outputPrefix}台账.json`
const browserPath = path.join(root, 'docs/完整待办与漏洞知识更新20260907/生产浏览器验收.md')
if (fs.existsSync(ledgerPath)) {
  const existing = JSON.parse(fs.readFileSync(ledgerPath, 'utf8'))
  if (existing.controls.some(row => row.current_release_evidence?.length || ['passed', 'failed'].includes(row.current_release_status))) {
    throw new Error('台账已包含新版生产实测；禁止初始化脚本覆盖，请保留原记录并人工合并新源码节点')
  }
}
const inventory = JSON.parse(fs.readFileSync(inventoryPath, 'utf8'))
const browserLines = fs.readFileSync(browserPath, 'utf8').split('\n')
const sha = value => createHash('sha256').update(value).digest('hex')
const property = (node, name) => node.properties.find(item => item.name?.getText() === name)?.initializer
const string = node => node && ts.isStringLiteralLike(node) ? node.text : null
const routerPath = path.join(frontend, 'src/router/index.ts')
const routeAst = ts.createSourceFile(routerPath, fs.readFileSync(routerPath, 'utf8'), ts.ScriptTarget.Latest, true)
let routeArray
function findRoutes(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText() === 'routes') routeArray = node.initializer
  ts.forEachChild(node, findRoutes)
}
findRoutes(routeAst)
if (!routeArray || !ts.isArrayLiteralExpression(routeArray)) throw new Error('未找到真实 routes 数组')
const routeRecords = []
const directHosts = new Map()
function addHost(file, host) {
  if (!directHosts.has(file)) directHosts.set(file, new Set())
  directHosts.get(file).add(host)
}
function walkRoutes(array, parentPath = '') {
  const descendantPaths = []
  for (const node of array.elements) {
    const segment = string(property(node, 'path'))
    if (segment === null) throw new Error('路由 path 不是已知静态字符串')
    const fullPath = segment.startsWith('/') ? segment : `${parentPath}/${segment}`.replaceAll(/\/+/g, '/')
    const component = property(node, 'component')
    let file = null
    if (component) {
      if (!ts.isArrowFunction(component) || !ts.isCallExpression(component.body)) throw new Error('未知路由组件形式')
      const imported = string(component.body.arguments[0])
      if (!imported?.startsWith('@/')) throw new Error('未知路由源码位置')
      file = `src/${imported.slice(2)}`
    }
    const children = property(node, 'children')
    const childPaths = children ? walkRoutes(children, fullPath) : []
    if (file) {
      const actualPaths = childPaths.length ? childPaths : [fullPath]
      for (const host of actualPaths) addHost(file, host)
      routeRecords.push({ path: fullPath, file, descendants: childPaths })
    }
    descendantPaths.push(...(childPaths.length ? childPaths : [fullPath]))
  }
  return descendantPaths
}
walkRoutes(routeArray)

const imports = new Map()
for (const entry of inventory.sources) {
  const filename = path.join(frontend, entry.file)
  const content = fs.readFileSync(filename, 'utf8')
  if (sha(content) !== entry.sha256) throw new Error(`清单源码漂移 ${entry.file}`)
  const { descriptor, errors } = parseSfc(content)
  if (errors.length) throw new Error(`SFC解析失败 ${entry.file}`)
  const children = []
  for (const block of [descriptor.script, descriptor.scriptSetup].filter(Boolean)) {
    const ast = ts.createSourceFile(filename, block.content, ts.ScriptTarget.Latest, true)
    for (const node of ast.statements) {
      if (!ts.isImportDeclaration(node)) continue
      const name = string(node.moduleSpecifier)
      if (!name?.endsWith('.vue')) continue
      const child = name.startsWith('@/') ? `src/${name.slice(2)}`
        : path.relative(frontend, path.resolve(path.dirname(filename), name))
      children.push(child)
    }
  }
  imports.set(entry.file, children)
}
const hosts = new Map()
function propagate(file, paths, seen = new Set()) {
  if (seen.has(file)) return
  const visited = new Set([...seen, file])
  if (!hosts.has(file)) hosts.set(file, new Set())
  for (const host of paths) hosts.get(file).add(host)
  for (const child of imports.get(file) || []) propagate(child, paths, visited)
}
for (const [file, paths] of directHosts) propagate(file, paths)

const accounts = [
  { role: 'owner_a', label: '项目A负责人', id: 103, scope: '专用项目162 owner', login: '已通过专用账号真实登录HTTP；本台账不推定新版浏览器已登录' },
  { role: 'member_a', label: '项目A普通成员', id: 104, scope: '专用项目162 reviewer，项目163不可见', login: '旧版生产浏览器与专用账号真实HTTP已登录' },
  { role: 'owner_b', label: '项目B负责人', id: 105, scope: '专用项目163 owner，项目162不可见', login: '已通过专用账号真实登录HTTP；本台账不推定新版浏览器已登录' },
  { role: 'no_permission', label: '无权限账号', id: 106, scope: '无RBAC角色/权限；个人功能按登录契约', login: '旧版生产浏览器与专用账号真实HTTP已登录' },
  { role: 'administrator', label: '管理员', id: null, scope: '实际管理员身份与超管资格待真实登录确认；不能混同两者', login: '待用户完成管理员生产浏览器登录' },
]
const history = new Map()
const unmapped = []
function attach(role, file, handler, snippet, kind = 'click', extra = '') {
  const matchingLines = browserLines.map((line, i) => ({ line, i })).filter(row => row.line.includes(snippet))
  const matchingActions = inventory.actions.filter(action => action.file === file && action.events.some(event => event.handler === handler))
  if (matchingLines.length !== 1 || matchingActions.length !== 1) {
    unmapped.push({ role, file, handler, snippet, reason: '证据行或源码节点不能唯一匹配，未推定' })
    return
  }
  const action = matchingActions[0]
  const entry = {
    environment: '生产 https://lijiadong.cn，旧版3.8.5，新版发布前',
    evidence_file: 'docs/完整待办与漏洞知识更新20260907/生产浏览器验收.md',
    evidence_line: matchingLines[0].i + 1,
    recorded_result: matchingLines[0].line,
    observation_type: kind,
    note: `${extra}仅旧版记录；不能替代新版复验。`,
  }
  const key = `${role}|${action.id}`
  if (!history.has(key)) history.set(key, [])
  history.get(key).push(entry)
}
attach('no_permission', 'src/views/dashboard/Dashboard.vue', 'goReviewList', '仪表盘“最近已完成审查 / 全部 →”', 'click_defect')
attach('no_permission', 'src/views/error/Forbidden.vue', 'goBack', '403返回上一页')
attach('no_permission', 'src/components/layout/AppHeader.vue', 'openSearch', '全局搜索打开、输入“报告”、关闭')
attach('no_permission', 'src/views/profile/ProfileCenter.vue', 'goChangePassword', '前往修改密码')
attach('no_permission', 'src/views/profile/ProfileCenter.vue', 'goApiConfig', '前往API配置')
attach('no_permission', 'src/views/profile/ChangePassword.vue', 'handleSubmit', '密码空表单确认修改', 'validation_only')
// 返回/取消两个按钮共用goBack；按确切按钮文本附加证据，不用共享handler强行映射。
attach('no_permission', 'src/views/profile/ApiConfig.vue', 'handleSave', 'API Key为空时保存', 'validation_only')
attach('no_permission', 'src/views/profile/ApiConfig.vue', 'handleTest', 'API Key为空时测试连接', 'disabled_observation')
attach('no_permission', 'src/views/profile/ApiConfig.vue', 'showKey = !showKey', '密钥显示切换按钮', 'accessibility_observation')
attach('no_permission', 'src/views/profile/ProfileCenter.vue', 'handleLogout', '退出/取消', 'confirmation_cancel')
attach('no_permission', 'src/views/profile/ProfileCenter.vue', 'handleLogout', '退出/确定', 'confirmation_confirm')
attach('member_a', 'src/views/project/ProjectList.vue', "view = 'table'", '表格/卡片')
attach('member_a', 'src/views/project/ProjectList.vue', "view = 'card'", '表格/卡片')
attach('member_a', 'src/views/project/ProjectList.vue', 'handleReset', '输入B项目关键字并回车')
attach('member_a', 'src/views/project/ProjectDetail.vue', 'openSecurityScan', '安全审计/开始扫描', 'click_defect')
attach('member_a', 'src/views/project/ProjectDetail.vue', 'openAddMemberDialog', '成员管理标签', 'visibility_defect', '仅观察可见，未点击添加。')
attach('member_a', 'src/views/project/ProjectDetail.vue', '(val: ProjectRole) => handleChangeRole(row.user_id, val)', '成员管理标签', 'visibility_defect', '仅观察角色控件，未修改。')
attach('member_a', 'src/views/project/ProjectDetail.vue', 'handleRemoveMember(row)', '成员管理标签', 'visibility_defect', '仅观察可见，未点击移除。')
attach('member_a', 'src/views/project/ProjectDetail.vue', 'handleUploadFile', '代码文件标签', 'visibility_defect', '未上传。')
attach('member_a', 'src/views/project/ProjectDetail.vue', 'handleUploadFolder', '代码文件标签', 'visibility_defect', '未上传。')
attach('member_a', 'src/views/code/CodeEditor.vue', 'handleSave', '查看代码|正确跳转', 'visibility_defect', '仅观察保存按钮与可编辑状态，未编辑/保存。')
// 模态框两处runScan、项目详情多种入口、登录表单/submit按钮不能从简短记录唯一确定，不挂接通过。
const rows = []
for (const account of accounts) {
  for (const action of inventory.actions) {
    const key = `${account.role}|${action.id}`
    rows.push({
      row_id: key,
      account_role: account.role, account_label: account.label, account_id: account.id,
      project_scope: account.scope,
      pages_from_source: [...(hosts.get(action.file) || [])].sort(),
      page_resolution: hosts.has(action.file) ? '路由AST与静态组件引用图；运行时宿主实例仍需实际确认' : '共享/动态组件，宿主尚未唯一解析',
      control_id: action.id, file: action.file, line: action.line, tag: action.tag,
      is_button_or_link: action.button, label: action.label,
      dynamic_accessible_label: action.bindings['aria-label'] || null,
      events: action.events, source_conditions: action.conditions,
      expected_behavior: '按账号真实RBAC、资源归属及当前业务状态验证允许/拒绝、可见/禁用；源码条件仅作定位线索',
      required_environment: '本轮最终源码经Git bundle发布后的生产HTTPS真实浏览器',
      current_release_status: account.role === 'administrator' ? 'blocked' : 'untested',
      blocker: account.role === 'administrator' ? account.login : null,
      current_release_actual_result: null, current_release_evidence: [],
      historical_observations: history.get(key) || [],
      scenarios_to_record: ['账号与页面前置状态', '可见或隐藏/禁用', '实际点击或交互', '确认/取消/错误与重试（适用时）', '最终跳转或数据读回', '发布版本及证据'],
    })
  }
}
if (rows.length !== accounts.length * inventory.action_count || new Set(rows.map(row => row.row_id)).size !== rows.length) throw Error('台账数量或ID错误')
const summary = {
  source_actions: inventory.action_count,
  source_buttons_or_links: inventory.button_count,
  roles: accounts.length,
  total_role_control_rows: rows.length,
  current_release_statuses: { untested: rows.filter(row => row.current_release_status === 'untested').length, blocked: rows.filter(row => row.current_release_status === 'blocked').length, passed: 0 },
  rows_with_historical_observations: rows.filter(row => row.historical_observations.length).length,
  historical_observations: rows.reduce((sum, row) => sum + row.historical_observations.length, 0),
  note: '源码节点×角色台账，不是运行时唯一按钮实例数；共享组件多宿主、列表重复行、原生组件内建关闭/确认和未绑定事件的输入需在浏览器补场景。旧版可见/禁用/校验不能当成点击成功或新版通过。',
}
const output = {
  generated_at_utc: new Date().toISOString(), summary, accounts,
  source_sha256: {
    [path.relative(root, inventoryPath)]: sha(fs.readFileSync(inventoryPath)),
    '生产浏览器验收.md': sha(fs.readFileSync(browserPath)),
    'frontend/src/router/index.ts': sha(fs.readFileSync(routerPath)),
  },
  unmapped_observations: unmapped,
  controls: rows,
}
fs.mkdirSync(path.dirname(outputPrefix), { recursive: true })
fs.writeFileSync(ledgerPath, JSON.stringify(output, null, 2) + '\n')
const columns = ['row_id','account_label','account_id','pages_from_source','file','line','tag','label','current_release_status','blocker','current_release_actual_result','historical_observations']
const csv = value => '"' + String(typeof value === 'object' && value !== null ? JSON.stringify(value) : value ?? '').replaceAll('"','""') + '"'
fs.writeFileSync(`${outputPrefix}台账.csv`, '\uFEFF'+[columns.map(csv).join(','), ...rows.map(row => columns.map(key => csv(row[key])).join(','))].join('\n')+'\n')
fs.writeFileSync(`${outputPrefix}汇总.json`, JSON.stringify({ ...summary, ledger_sha256: sha(fs.readFileSync(ledgerPath)) }, null, 2)+'\n')
console.log(JSON.stringify(summary))
