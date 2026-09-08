// 执行当前SFC中的原函数，注入本地数据/拒绝/延迟；不发HTTP，不挂载生产UI。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import assert from 'node:assert/strict'
const evidence = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(evidence, '../../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const ts = require('typescript')
const { parse } = require('@vue/compiler-sfc')
const sources = {}
const results = []
const ref = value => ({ value })
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function extract(file, names, dependencies) {
  const source = fs.readFileSync(path.join(root, file), 'utf8')
  sources[file] = createHash('sha256').update(source).digest('hex')
  const { descriptor, errors } = parse(source)
  assert.equal(errors.length, 0)
  const script = descriptor.scriptSetup.content
  const ast = ts.createSourceFile(file, script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const nodes = names.map(name => ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name))
  assert(nodes.every(Boolean))
  const compiled = ts.transpileModule(nodes.map(node => node.getText(ast)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText
  return new Function(...Object.keys(dependencies), compiled + '\nreturn {' + names.join(',') + '}')(...Object.values(dependencies))
}
const projectFile = 'frontend/src/views/project/ProjectList.vue'
const codeFile = 'frontend/src/views/code/CodeFileList.vue'
const detailFile = 'frontend/src/views/project/ProjectDetail.vue'
const formFile = 'frontend/src/views/project/ProjectForm.vue'
const agentFile = 'frontend/src/views/agent/AgentCenter.vue'
const reportFile = 'frontend/src/views/report/ReportDetail.vue'
const projectState = () => ({ loading: ref(false), projects: ref([]), total: ref(0), page: ref(1), pageSize: ref(10), keyword: ref(''), languageFilter: ref(''), statusFilter: ref('') })
{
  const state = projectState(); let count = 0
  const { fetchProjects } = extract(projectFile, ['fetchProjects'], { ...state, getProjects: async () => { if (++count === 1) return { items: [{ id: 162 }], total: 1 }; throw new Error('injected-503') } })
  await fetchProjects(); const before = structuredClone(state.projects.value); await fetchProjects()
  assert.equal(before.length, 1); assert.equal(state.projects.value.length, 0); assert.equal(state.total.value, 0); assert.equal(state.loading.value, false)
  results.push({ id: 'F01', reproduced: true, file: projectFile, function: 'fetchProjects', scenario: '已有成功结果后刷新失败', actual: { before, after: state.projects.value, total: state.total.value, loading: state.loading.value }, problem: '读取失败清空已有项目，显示层失去错误与空结果区别' })
}
{
  const state = projectState(); const old = deferred(); const latest = deferred()
  const { fetchProjects } = extract(projectFile, ['fetchProjects'], { ...state, getProjects: params => params.keyword === 'A' ? old.promise : latest.promise })
  state.keyword.value = 'A'; const first = fetchProjects(); state.keyword.value = 'B'; const second = fetchProjects()
  latest.resolve({ items: [{ id: 163, name: 'B' }], total: 1 }); await second
  old.resolve({ items: [{ id: 162, name: 'A' }], total: 1 }); await first
  assert.equal(state.keyword.value, 'B'); assert.equal(state.projects.value[0].name, 'A')
  results.push({ id: 'F02', reproduced: true, file: projectFile, function: 'fetchProjects', scenario: 'A慢请求在B快请求之后完成', actual: { query: state.keyword.value, rows: state.projects.value }, problem: '旧筛选响应覆盖新筛选结果' })
}
{
  const state = { canView: ref(true), loading: ref(false), files: ref([{ id: 1892 }]), total: ref(1), page: ref(1), pageSize: ref(10), languageFilter: ref(''), props: { projectId: 162 } }
  const { fetchFiles } = extract(codeFile, ['fetchFiles'], { ...state, list: async () => { throw new Error('injected-503') } })
  await fetchFiles(); assert.equal(state.files.value.length, 0); assert.equal(state.total.value, 0); assert.equal(state.loading.value, false)
  results.push({ id: 'F03', reproduced: true, file: codeFile, function: 'fetchFiles', scenario: '已有文件列表后读取失败', actual: { files: state.files.value, total: state.total.value, loading: state.loading.value }, problem: '故障伪装成暂无代码文件' })
}
{
  const state = { canViewProject: ref(true), loading: ref(false), project: ref({ id: 162, can_update: true }), projectId: 162 }
  const { fetchDetail } = extract(detailFile, ['fetchDetail'], { ...state, getProjectDetail: async () => { throw new Error('injected-503') } })
  await fetchDetail(); assert.equal(state.project.value, null); assert.equal(state.loading.value, false)
  results.push({ id: 'F04', reproduced: true, file: detailFile, function: 'fetchDetail', scenario: '已有详情后再次读取失败', actual: { project: state.project.value, loading: state.loading.value }, problem: '同资源刷新失败清空详情及所有依赖按钮' })
}
{
  const pending = []; const parent = extract(projectFile, ['onFormSubmit'], {
    formMode: ref('create'), canCreateProject: ref(true), canUpdateProject: ref(true), editingProject: ref(null), formVisible: ref(true),
    createProject: () => { const d = deferred(); pending.push(d); return d.promise }, updateProject: async () => {}, uploadFolder: async () => {},
    ElMessage: { success() {}, info() {}, error() {}, warning() {} }, fetchProjects: async () => {},
  })
  const submitting = ref(false); const parentOperations = []
  const { handleSubmit } = extract(formFile, ['handleSubmit'], {
    formRef: ref({ validate: async () => true }), submitting, form: { project_name: 'local-only-project', description: '', language: '' }, selectedFiles: ref([]),
    emit: (_name, data) => { parentOperations.push(parent.onFormSubmit(data)) },
  })
  await handleSubmit(); const whileFirstUnresolved = submitting.value; await handleSubmit()
  assert.equal(pending.length, 2); assert.equal(whileFirstUnresolved, false)
  results.push({ id: 'F05', reproduced: true, files: [formFile, projectFile], function: 'handleSubmit + onFormSubmit', scenario: '第一次创建尚未返回时再次提交', actual: { creationCalls: pending.length, submittingWhileCreationPending: whileFirstUnresolved }, problem: '子表单只等待emit，实际创建期间不锁定，父回调也没有防重入' })
  pending.forEach((d, index) => d.resolve({ id: 10000 + index })); await Promise.all(parentOperations)
}
{
  const form = { project_name: '用户手工名称', description: '用户手工描述', language: 'python' }; const aiFilled = ref(false); const analyzing = ref(false)
  const { autoAnalyzeFolder } = extract(formFile, ['autoAnalyzeFolder'], { form, aiFilled, analyzing, analyzeFolder: async () => { throw new Error('injected-503') }, ElMessage: { success() {}, warning() {} } })
  await autoAnalyzeFolder('selected-folder', ['a.py'])
  assert.deepEqual(form, { project_name: 'selected-folder', description: '', language: 'plaintext' }); assert.equal(aiFilled.value, true)
  results.push({ id: 'F06', reproduced: true, file: formFile, function: 'autoAnalyzeFolder', scenario: '分析失败时已有人工填写内容', actual: { form, aiFilled: aiFilled.value, analyzing: analyzing.value }, problem: '失败覆盖人工内容且将aiFilled设true，触发成功分析横幅' })
}
{
  const messages = []; const runtime = ref([{ code: 'old' }]); const situation = ref({ online: 1 }); const typeMappings = ref([]); const loading = ref(false)
  const { refreshAll } = extract(agentFile, ['loadAll', 'refreshAll'], { runtime, situation, typeMappings, loading,
    listRuntimeAgents: async () => [{ code: 'new' }], getSituation: async () => ({ online: 1 }), listTypeMappings: async () => { throw new Error('injected-noncritical-mapping-error') },
    syncSituationActivityCounts() {}, ElMessage: { error: message => messages.push({ type: 'error', message }), success: message => messages.push({ type: 'success', message }) },
  })
  await refreshAll(); assert.deepEqual(messages.map(item => item.type), ['error', 'success']); assert.equal(runtime.value[0].code, 'old')
  results.push({ id: 'F07', reproduced: true, file: agentFile, function: 'loadAll + refreshAll', scenario: '非关键画像映射失败，runtime和态势请求成功', actual: { messages, runtime: runtime.value, loading: loading.value }, problem: '成功部分未展示，且刷新失败后错误提示紧接成功提示' })
}
{
  const old = deferred(); const fresh = deferred(); const agentSkills = ref([]); const skillsLoading = ref(false)
  const { loadAgentSkills } = extract(agentFile, ['loadAgentSkills'], { agentSkills, skillsLoading, listAgentSkills: code => code === 'A' ? old.promise : fresh.promise })
  const first = loadAgentSkills('A'); const second = loadAgentSkills('B'); fresh.resolve([{ name: 'B.self_improve' }]); await second; old.resolve([{ name: 'A.self_improve' }]); await first
  assert.equal(agentSkills.value[0].name, 'A.self_improve')
  results.push({ id: 'F08', reproduced: true, file: agentFile, function: 'loadAgentSkills', scenario: '切到AgentB后旧AgentA请求才结束', actual: { selectedByScenario: 'B', displayedSkills: agentSkills.value }, problem: '旧Agent技能响应覆盖当前抽屉的技能' })
}
{
  const report = ref({ task_id: 161 }); const loading = ref(false); const messages = []
  const { loadReport } = extract(reportFile, ['loadReport'], { report, loading, taskId: 161, getReportDetail: async () => { throw new Error('injected-503') }, ElMessage: { error: message => messages.push(message) } })
  await loadReport(); assert.equal(report.value, null); assert.equal(loading.value, false)
  results.push({ id: 'F09', reproduced: true, file: reportFile, function: 'loadReport', scenario: '同报告已显示后读取失败', actual: { report: report.value, loading: loading.value, messages }, problem: '只有返回列表重试提示，原报告清空；是否存在可达刷新触发需挂载验收进一步确认' })
}
{
  const downloadingId = ref(null); const messages = []
  const { handleDownload } = extract(codeFile, ['handleDownload'], { canDownload: ref(true), downloadingId, downloadBinary: async () => ({ localFakeBlob: true }),
    URL: { createObjectURL: () => { throw new Error('injected-browser-object-url-failure') } },
    ElMessage: { success: message => messages.push(message), error: message => messages.push(message) },
  })
  await handleDownload({ id: 1892, file_name: 'local-fake.bin' }); assert.equal(messages.length, 0); assert.equal(downloadingId.value, null)
  results.push({ id: 'F10', reproduced: true, file: codeFile, function: 'handleDownload', scenario: 'API已返回但本地URL创建抛异常', actual: { messages, downloadingId: downloadingId.value, actualDownload: false }, problem: '本地异常也被“拦截器已处理”catch吞掉，用户没有反馈' })
}
const report = { observed_at_utc: new Date().toISOString(), method: 'current_sfc_function_execution_with_local_injected_dependencies', production_requests: 0, model_requests: 0, downloads: 0, ui_click_passes: 0, application_changes: false, sources, cases: results }
fs.writeFileSync(path.join(evidence, '前端失败路径原函数复现.json'), JSON.stringify(report, null, 2) + '\n')
console.log(JSON.stringify({ cases: results.length, reproduced: results.filter(item => item.reproduced).length, production_requests: 0, model_requests: 0, downloads: 0 }))
