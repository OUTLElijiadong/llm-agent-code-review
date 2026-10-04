import { flushPromises, shallowMount, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({ projects: vi.fn(), projectDetail: vi.fn(), issues: vi.fn(), listSandboxes: vi.fn(), getSandbox: vi.fn(), createSandbox: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: mocks.projects, getProjectDetail: mocks.projectDetail }))
vi.mock('@/api/issue', () => ({ list: mocks.issues, updateStatus: vi.fn(), batchUpdateStatus: vi.fn() }))
vi.mock('@/api/sandbox', () => ({ listSandboxes: mocks.listSandboxes, getSandbox: mocks.getSandbox, createSandbox: mocks.createSandbox, authorizeSandboxRemoteTarget: vi.fn(), stopSandbox: vi.fn(), extendSandbox: vi.fn(), createSandboxPreviewSession: vi.fn(), searchSandboxCapabilities: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/mcpGovernance', () => ({ listSandboxWorkers: vi.fn().mockResolvedValue([]) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: () => false, isSuperAdmin: () => false }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: {success:vi.fn(), warning:vi.fn(), error:vi.fn()} }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: {confirm:vi.fn()} }))
import IssueHub from '@/views/issue/IssueHub.vue'
import SandboxWorkstation from '@/views/sandbox/SandboxWorkstation.vue'
const Slot = { template: '<div><slot /></div>' }
const stubs = { ElForm: Slot, ElFormItem: Slot, ElCheckbox: Slot, ElRadioButton: Slot, ElRadioGroup: Slot, ElInputNumber: Slot, ElEmpty: Slot, ElCard: Slot, ElSelect: Slot, ElOption: Slot, ElInput: Slot, ElTag: Slot, ElIcon: Slot, ElPagination: Slot, ElDropdown: Slot, ElDropdownMenu: Slot, ElDropdownItem: Slot, ElAlert: Slot, ElButton: Slot, EmptyState: Slot }
function deferred<T>() { let resolve!: (v:T)=>void; const promise = new Promise<T>(done=>{resolve=done}); return {promise, resolve} }
function issue(id:number,title:string){return {id,title, task_id:id,severity:'中',status:'unfixed',issue_type:'安全漏洞'}}
const environment = { public_id: 'sbx_audit', project_id:1, owner_id:2, worker_code:'managed', agent_code:'test_verifier', purpose:'test', language:'python', test_mode:'whitebox',status:'running',runtime:'runsc',source_sha256:'a'.repeat(64),expires_at:'2026-10-01T00:00:00Z',events:[] }
beforeEach(()=>{vi.clearAllMocks(); mocks.projects.mockResolvedValue({items:[{id:1,project_name:'A',status:'active',language:'python'},{id:2,project_name:'B',status:'active',language:'node'}],total:2}); mocks.issues.mockResolvedValue({items:[],total:0}); mocks.listSandboxes.mockResolvedValue([environment]); mocks.getSandbox.mockResolvedValue(environment); mocks.projectDetail.mockResolvedValue({source_revisions:[]})})
afterEach(()=>vi.useRealTimers())
describe('异步组件一致性回归',()=>{
 it('问题筛选：迟到的项目 A 响应覆盖当前项目 B',async()=>{
  const wrapper=mount(IssueHub,{global:{directives:{loading:()=>{}},stubs}}); await flushPromises(); const vm=wrapper.vm as any;
  const old=deferred<any>(),fresh=deferred<any>(); mocks.issues.mockImplementation((p:any)=>p.project_id===1?old.promise:fresh.promise);
  vm.filters.project_id=1; const a=vm.loadIssues(); vm.filters.project_id=2; const b=vm.loadIssues(); fresh.resolve({items:[issue(2,'项目B结果')],total:1}); await b; old.resolve({items:[issue(1,'项目A过期结果')],total:1}); await a; await nextTick();
  expect(vm.filters.project_id).toBe(2); expect(wrapper.text()).toContain('项目B结果'); expect(wrapper.text()).not.toContain('项目A过期结果'); wrapper.unmount();
 });
 it('问题读取：403 后仍保留先前敏感结果',async()=>{
  mocks.issues.mockResolvedValue({items:[issue(1,'原先可见源码问题')],total:1}); const wrapper=mount(IssueHub,{global:{directives:{loading:()=>{}},stubs}});await flushPromises();mocks.issues.mockRejectedValue({code:40301,message:'没有查看权限'});await (wrapper.vm as any).loadIssues();await nextTick(); expect(wrapper.text()).not.toContain('原先可见源码问题');expect((wrapper.vm as any).total).toBe(0);expect((wrapper.vm as any).selected).toEqual([]);expect(wrapper.text()).toContain('旧问题已清空');wrapper.unmount();
 });
 it('沙箱项目：迟到副本 A 覆盖项目 B 副本列表',async()=>{
  const wrapper=shallowMount(SandboxWorkstation,{global:{directives:{loading:()=>{}},stubs}});await flushPromises();const vm=wrapper.vm as any;
  const a=deferred<any>(),b=deferred<any>();mocks.projectDetail.mockImplementation((id:number)=>id===1?a.promise:b.promise);
  vm.form.project_id=null;await nextTick();vm.form.project_id=1;await nextTick();vm.form.project_id=2;await nextTick();b.resolve({source_revisions:[{id:202,revision_no:2,repaired_files:[]}]});await flushPromises();a.resolve({source_revisions:[{id:101,revision_no:1,repaired_files:[]}]});await flushPromises();expect(vm.form.project_id).toBe(2);expect(vm.sourceRevisions[0].id).toBe(202);wrapper.unmount();
 });
 it('沙箱离页：加载完成晚于 unmount 后仍启动轮询',async()=>{
  vi.useFakeTimers(); const p=deferred<any>();mocks.projects.mockReturnValue(p.promise);const wrapper=shallowMount(SandboxWorkstation,{global:{directives:{loading:()=>{}},stubs}});await nextTick();wrapper.unmount();p.resolve({items:[{id:1,project_name:'A',status:'active',language:'python'}],total:1});await flushPromises();const prior=mocks.listSandboxes.mock.calls.length;await vi.advanceTimersByTimeAsync(2500);await flushPromises();expect(mocks.listSandboxes.mock.calls.length).toBe(prior);vi.clearAllTimers();
 });
})
