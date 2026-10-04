import {flushPromises, shallowMount} from '@vue/test-utils'
import {createPinia,setActivePinia} from 'pinia'
import {beforeEach,describe,it,expect,vi} from 'vitest'
const api=vi.hoisted(()=>({retry:vi.fn().mockResolvedValue({}),cancel:vi.fn().mockResolvedValue({}),confirm:vi.fn()}))
vi.mock('@/api/agentTeams',()=>({retryAgentTeam:api.retry,cancelAgentTeam:api.cancel}))
vi.mock('element-plus/es/components/message-box/index',()=>({ElMessageBox:{confirm:api.confirm}}))
vi.mock('element-plus/es/components/message/index',()=>({ElMessage:{success:vi.fn(),error:vi.fn()}}))
import TeamWindow from '@/components/ai/AgentTeamWindow.vue'
function team(id:number){return{team_id:id,title:`团队${id}`,status:'running',surface:'user',session_id:'S',counts:{total:1,completed:0,running:1,queued:0,failed:0,blocked:0},members:[],tasks:[{task_id:id,task_key:`failed-${id}`,status:'failed',depends_on:[],attempt_count:1,max_attempts:3}],messages:[],events:[]}}
function deferred(){let resolve!:()=>void;const promise=new Promise<void>(done=>{resolve=done});return{promise,resolve}}
const options={global:{stubs:{Teleport:true,Transition:false,ElIcon:true,Close:true}}}
beforeEach(()=>setActivePinia(createPinia()))
describe('团队操作目标冻结缺口',()=>{
 it.each(['cancelTeam','retryFailed'])('%s 确认期间切换后实际提交另一团队',async(method)=>{api.retry.mockClear();api.cancel.mockClear();const d=deferred();api.confirm.mockReturnValueOnce(d.promise);const wrapper=shallowMount(TeamWindow,{...options,props:{visible:true,team:team(1) as any}});const action=(wrapper.vm as any)[method]();await wrapper.setProps({team:team(2) as any});d.resolve();await action;await flushPromises();const called=method==='cancelTeam'?api.cancel:api.retry;expect(called.mock.calls[0][0]).toBe(2);if(method==='retryFailed')expect(called.mock.calls[0][1]).toEqual(['failed-2']);console.log('REPRO',method,'confirmed team1, mutated team2');wrapper.unmount()})
})
it('同一团队后台同步改变失败集合后，重试包含未经确认的新任务',async()=>{api.retry.mockClear();const d=deferred();api.confirm.mockReturnValueOnce(d.promise);const initial=team(7);initial.tasks[0].task_key='originally-failed';const wrapper=shallowMount(TeamWindow,{...options,props:{visible:true,team:initial as any}});const action=(wrapper.vm as any).retryFailed();const synced=team(7);synced.tasks[0].task_key='newly-failed';await wrapper.setProps({team:synced as any});d.resolve();await action;expect(api.retry).toHaveBeenCalledWith(7,['newly-failed']);wrapper.unmount()})
it('团队详情消息超过160字符后无完整payload展示入口',async()=>{const sample=team(9) as any;sample.messages=[{message_id:'message-long',sent_from:'agent:test_verifier',message_type:'task.result',subject:'完整审查结果',payload:'x'.repeat(170)+'TAIL_EVIDENCE_关键复现依据',status:'completed',create_time:'2026-10-01T00:00:00Z'}];const wrapper=shallowMount(TeamWindow,{...options,props:{visible:true,team:sample}});expect(wrapper.get('.team-chat-payload').text()).toBe('x'.repeat(160)+'...');expect(wrapper.text()).not.toContain('TAIL_EVIDENCE_关键复现依据');expect(wrapper.get('.team-chat-row').find('details').exists()).toBe(false);wrapper.unmount()})
