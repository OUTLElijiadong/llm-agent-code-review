import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Plugin } from 'vue'

const streams = vi.hoisted(() => ({
  start: vi.fn(),
  records: [] as Array<{
    body: Record<string, unknown>
    onEvent: (event: unknown) => void
    resolve: () => void
    reject: (error: unknown) => void
    aborted: boolean
  }>,
}))

const messages = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn() }))
const sessionApi = vi.hoisted(() => ({ get: vi.fn(), page: vi.fn() }))
const meshApi = vi.hoisted(() => ({ heartbeat: vi.fn(), inbox: vi.fn(), list: vi.fn() }))
const teamApi = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), messages: vi.fn(), events: vi.fn() }))
const responseApi = vi.hoisted(() => ({ cancel: vi.fn() }))
const connectedDisclosureRoots = new Set<Element>()

vi.mock('vue-router', async (original) => ({ ...await original<typeof import('vue-router')>(), useRouter: () => ({ push: vi.fn(), resolve: vi.fn() }) }))
vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({
  getAgentResponseSession: sessionApi.get,
  getAgentResponseSessionMessages: sessionApi.page,
  cancelAgentResponseRun: responseApi.cancel,
}))
vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: meshApi.heartbeat,
  pullAgentMeshInbox: meshApi.inbox,
  listAgentMeshAgents: meshApi.list,
}))
vi.mock('@/api/agentTeams', () => ({
  listAgentTeams: teamApi.list,
  getAgentTeam: teamApi.detail,
  listAgentTeamMessages: teamApi.messages,
  listAgentTeamEvents: teamApi.events,
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import AgentChatDrawer from '@/components/ai/AgentChatDrawer.vue'
import { useAgentActivityStore } from '@/stores/agentActivity'
import { useUserStore } from '@/stores/user'
import {
  agentChatStorageKey,
  loadAgentChatSnapshot,
  saveActiveAgentChatSession,
  saveAgentChatSessions,
  saveAgentChatSnapshot,
} from '@/utils/agentChatSessions'

function mountDrawer(prefill?: string, extraPlugins: Plugin[] = []): VueWrapper {
  return mount(AgentChatDrawer, {
    props: { visible: true, prefill },
    global: {
      plugins: extraPlugins,
      stubs: {
        Teleport: true,
        Transition: false,
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        'el-button': { template: '<button><slot /></button>' },
        'el-input': true,
        'el-input-number': true,
        'el-option': true,
        'el-select': true,
        AgentAvatar: true,
        AgentNavLink: { props: ['href', 'label', 'hint', 'prominent'], template: '<button class="agent-nav-link-stub">{{ label }}</button>' },
        Check: true,
        CircleCheck: true,
        CircleCloseFilled: true,
        Close: true,
        Connection: true,
        CopyDocument: true,
        Loading: true,
        Promotion: true,
        WarningFilled: true,
      },
    },
  })
}

function emit(index: number, event: Record<string, unknown>): void {
  streams.records[index].onEvent(event)
}

async function finish(index: number): Promise<void> {
  streams.records[index].resolve()
  await flushPromises()
}

/** 模拟流异常中断(网络错误等),复现 fetch 层的 AbortError 拒绝。 */
async function failStream(index: number, error: unknown): Promise<void> {
  streams.records[index].reject(error)
  await flushPromises()
  // 等待 runResponse 的 finally 收尾(loading 复位、错误卡片入流)
  await flushPromises()
}

/** 等所有微任务与一轮宏任务跑完(setTimeout 回调、watch 后置刷新等)。 */
async function settleAll(): Promise<void> {
  await flushPromises()
  await new Promise<void>((resolve) => { setTimeout(resolve, 0) })
  await flushPromises()
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  // 组件在 setup 顶层使用 useAgentActivityStore,测试环境需先激活 Pinia
  setActivePinia(createPinia())
  meshApi.heartbeat.mockReset().mockResolvedValue({})
  meshApi.inbox.mockReset().mockResolvedValue([])
  meshApi.list.mockReset().mockResolvedValue({ items: [], total: 0, by_kind: {} })
  teamApi.list.mockReset().mockResolvedValue({ items: [], total: 0 })
  teamApi.detail.mockReset()
  teamApi.events.mockReset().mockResolvedValue({
    items: [], has_more: false, next_after_id: 0, page_size: 200, team_status: 'running',
  })
  sessionApi.get.mockReset()
  sessionApi.page.mockReset()
  sessionApi.get.mockResolvedValue({
    surface: 'user', session_id: 'user-test', run: null, messages: [], pending: null,
  })
  streams.start.mockReset()
  streams.records.splice(0)
  streams.start.mockImplementation((
    body: Record<string, unknown>,
    options: { onEvent: (event: unknown) => void },
  ) => {
    const record = {
      body,
      onEvent: options.onEvent,
      resolve: (): void => undefined,
      reject: (): void => undefined,
      aborted: false,
    }
    const done = new Promise<void>((doneResolve, doneReject) => {
      record.resolve = doneResolve
      record.reject = doneReject
    })
    // 测试自行处理拒绝场景,避免未处理的 Promise 告警
    done.catch(() => undefined)
    streams.records.push(record)
    return {
      abort: () => {
        record.aborted = true
        const error = new Error('The operation was aborted')
        error.name = 'AbortError'
        ;(record as { reject: (error: unknown) => void }).reject(error)
      },
      signal: new AbortController().signal,
      done,
    }
  })
  responseApi.cancel.mockReset().mockResolvedValue({ status: 'cancelled' })
})

afterEach(() => {
  const leakedRoots = [...connectedDisclosureRoots].filter((root) => root.isConnected)
  for (const root of connectedDisclosureRoots) root.remove()
  connectedDisclosureRoots.clear()
  expect(leakedRoots).toEqual([])
  vi.useRealTimers()
})

async function mountReadyDrawer(prefill?: string, extraPlugins: Plugin[] = []): Promise<VueWrapper> {
  const wrapper = mountDrawer(prefill, extraPlugins)
  await flushPromises()
  return wrapper
}

/** 过程摘要默认折叠；集成断言先通过可见控件真实展开。 */
function detail(id:number,status='running'){return{team_id:id,title:`团队${id}`,surface:'user',session_id:'user-test',status,max_active_children:3,trace_id:`trace-${id}`,counts:{total:2,completed:status==='completed'?2:0,running:status==='running'?2:0,queued:0,failed:0,blocked:0},members:[],tasks:[],events:[],messages:[]}}
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return{promise,resolve}}
it('局部同步失败保留已展示团队并明确提示快照暂时陈旧',async()=>{const wrapper=await mountReadyDrawer();const vm=wrapper.vm as any;vm.sessionId='user-test';teamApi.list.mockResolvedValue({items:[detail(1),detail(2)],total:2});teamApi.detail.mockImplementation((id:number)=>Promise.resolve(detail(id)));await vm.refreshAgentTeam();expect(vm.agentTeams.length).toBe(2);teamApi.detail.mockImplementation((id:number)=>id===1?Promise.resolve(detail(1)):Promise.reject(new Error('brief network error')));await vm.refreshAgentTeam();expect(vm.agentTeams.map((x:any)=>x.team_id)).toEqual([1,2]);expect(vm.agentTeams.find((x:any)=>x.team_id===2).status).toBe('running');expect(vm.cachedAgentTeams.map((x:any)=>x.team_id)).toEqual([1,2]);expect(vm.agentTeamError).toContain('1 个团队状态暂时未同步');wrapper.unmount()})
it('同会话迟到的旧团队响应不得覆盖新完成状态',async()=>{const wrapper=await mountReadyDrawer();const vm=wrapper.vm as any;vm.sessionId='user-test';teamApi.list.mockResolvedValue({items:[detail(1)],total:1});const old=deferred<any>();let n=0;teamApi.detail.mockImplementation(()=>++n===1?old.promise:Promise.resolve(detail(1,'completed')));const a=vm.refreshAgentTeam();await flushPromises();const b=vm.refreshAgentTeam();await b;expect(vm.agentTeams[0].status).toBe('completed');old.resolve(detail(1,'running'));await a;expect(vm.agentTeams[0].status).toBe('completed');wrapper.unmount()})
