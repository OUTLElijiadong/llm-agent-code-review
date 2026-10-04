"""只读架构探针：仅内存 SQLite、模型桩、线程事件，不外呼模型/生产，不写应用代码。"""
from __future__ import annotations
import asyncio, hashlib, importlib.util, json, re, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'backend'))
spec=importlib.util.spec_from_file_location('audit_conftest',ROOT/'backend/tests/conftest.py')
conf=importlib.util.module_from_spec(spec);spec.loader.exec_module(conf)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.user import User
from app.models.agent_team import AgentTeam,AgentTeamTask,AgentTeamEvent
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_team_service as ats,agent_team_dispatcher as atd,agent_supervisor_service as sup

def db_new():
 e=create_engine('sqlite:///:memory:',connect_args={'check_same_thread':False});Base.metadata.create_all(e)
 return sessionmaker(bind=e,expire_on_commit=False,autoflush=False)()

def high_retry():
 db=db_new(); user=User(id=1,username='admin',password='x',role='super_admin',status=1);db.add(user);db.commit()
 payload=AgentTeamCreateIn.model_validate({'surface':'admin','session_id':'audit_high_retry','title':'只读运维','objective':'状态检查','max_attempts':2,'members':[{'member_key':'ops','display_name':'ops','address':'agent:operations','role':'worker'},{'member_key':'summary','display_name':'summary','address':'agent:reporter','role':'summarizer'}], 'tasks':[{'task_key':'ops','member_key':'ops','title':'服务器状态','instructions':'执行只读运维状态查询','max_attempts':2,'input':{'action':'status','params':{}}},{'task_key':'summary','member_key':'summary','title':'汇总','instructions':'汇总已完成结果','depends_on':['ops']}]})
 with patch('app.services.rbac_service.is_super_admin_user',lambda *_a:True):
  review=sup.review_agent_team_plan(payload.model_dump(mode='json')); created=ats.create_team_from_xiaoling(db,user,payload,supervisor_plan_sha256=review['plan_sha256'],supervisor_confirmed_by=user.id)
 claim=ats.claim_next_task(db,created['team_id']); oldfp=review['tasks'][0]['fingerprint']
 ats.complete_task(db,created['team_id'],claim['task_id'],lease_token=claim['lease_token'],result={'status':'failed','summary':'temporary timeout','retryable':True},success=False,error='temporary timeout')
 claim2=ats.claim_next_task(db,created['team_id']); fp2=sup.task_fingerprint({'task_key':claim2['task_key'],'member_key':'ops','title':claim2['title'],'instructions':claim2['instructions'],'input':claim2['input']},claim2['address'])
 recorded=[]
 def fake_complete(*a,**kw):recorded.append(kw);return {}
 with patch.object(atd,'SessionLocal',lambda:db),patch.object(atd,'_owner_access_error',lambda *_a:''),patch.object(ats,'record_supervisor_task_review',lambda *_a,**_kw:None),patch.object(ats,'complete_task',fake_complete),patch('app.services.agent_mesh_dispatcher._handle',side_effect=AssertionError('must not execute')):
  outcome=atd._execute_claimed(created['team_id'],claim2)
 print(json.dumps({'scenario':'high_risk_retry','initially_confirmed':True,'task_queued_attempt':claim2['attempt_count'],'fingerprint_changed':oldfp!=fp2,'executed':outcome['success'],'blocked_code':recorded[0]['result']['errors'][0]['code']},ensure_ascii=False))

def wave():
 class DB:
  def rollback(self):pass
  def expire_all(self):pass
  def close(self):pass
 short_done=threading.Event();release_long=threading.Event();events=[];lock=threading.Lock(); claimed=set()
 def claim(_db,team_id,**kw):
  with lock:
   for key in ['short','long','after_short']:
    if key in claimed or (key=='after_short' and not short_done.is_set()):continue
    claimed.add(key);events.append('claim:'+key);return {'task_id':len(claimed),'task_key':key}
   return None
 def execute(team_id,c):
  k=c['task_key'];events.append('start:'+k)
  if k=='short':short_done.set();events.append('done:short')
  elif k=='long':release_long.wait(3);events.append('done:long')
  return {'success':True}
 patches=[patch.object(atd,'SessionLocal',lambda:DB()),patch.object(atd,'_candidate_teams',lambda *_a:[1]),patch.object(atd.settings,'agent_team_enabled',True),patch.object(atd.settings,'agent_team_max_active_children',2),patch.object(ats,'expire_due_teams',lambda *_a:0),patch.object(ats,'recover_expired_leases',lambda *_a:0),patch.object(ats,'cleanup_terminal_team_resources',lambda *_a:0),patch.object(ats,'claim_next_task',claim),patch.object(atd,'_execute_claimed',execute)]
 for p in patches:p.start()
 try:
  t=threading.Thread(target=lambda:atd.dispatch_once(limit=2));t.start();short_done.wait(2);time.sleep(.1)
  while_long_running='after_short' in claimed;release_long.set();t.join(3)
  print(json.dumps({'scenario':'dag_wave_barrier','short_finished':short_done.is_set(),'dependent_claimed_while_long_running':while_long_running,'dependent_claimed_after_long_finished':'after_short' in claimed,'events':events},ensure_ascii=False))
 finally:
  release_long.set()
  for p in reversed(patches):p.stop()

async def semantic():
 from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime,InMemoryCheckpointStore
 critical='统计时区是 Asia/Taipei，月末退款按原始交易月份记账。'
 class Transport:
  def __init__(self):self.payloads=[]
  async def create_response(self,payload):
   self.payloads.append(payload)
   if not payload['tools']:
    source=str(payload['input'][0]['content']);spans=list(re.finditer(r'\[来源#\d+:片段\d+/\d+\]',source)); quotes=[];ids=[]
    for i,s in enumerate(spans):
     source_id=s.group()[1:-1];ids.append(source_id);end=spans[i+1].start() if i+1<len(spans) else len(source);piece=source[s.end():end].strip();piece=re.sub(r'^\[来源角色=[^\]]+\]\s*','',piece);quotes.append({'source_id':source_id,'quote':piece[:24]})
    text=json.dumps({'covered_source_ids':ids,'source_quotes':quotes,'summary':'所有来源已读。'},ensure_ascii=False)
   else:text='已完成核查'
   return {'id':'audit_semantic','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}
 transcript=[{'role':'user','content':'检查交易逻辑'}]+[{'role':'assistant','content':'背景甲'*90} for _ in range(8)]+[{'role':'user','content':'中间背景'*80+critical}]+[{'role':'assistant','content':'背景乙'*90} for _ in range(8)]+[{'role':'user','content':'继续'}]
 transport=Transport();runtime=DeepSeekResponsesRuntime(transport=transport,tool_executor=SimpleNamespace(),checkpoint_store=InMemoryCheckpointStore(),context_window_tokens=6000,max_output_tokens=400,compaction_threshold_tokens=500,keep_recent_tokens=200,stream=False)
 result=await runtime.start(transcript,run_id='arch_semantic_omission')
 original_reached_compactor=any(critical in json.dumps(p,ensure_ascii=False) for p in transport.payloads[:-1]);final=json.dumps(transport.payloads[-1]['input'],ensure_ascii=False)
 print(json.dumps({'scenario':'semantic_omission','result_status':result.status,'original_fact_reached_compactor':original_reached_compactor,'fact_in_final_model_input':critical in final,'compactor_calls':len(transport.payloads)-1},ensure_ascii=False))



def delegate_version_drift():
 from app.models.custom_agent import CustomAgentRelease
 from app.services import agent_studio_service as studio, approval_service
 from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory as F
 db=db_new()
 reviewer=User(username='audit_reviewer',password='x',role='reviewer',status=1)
 admin=User(username='admin',password='x',role='super_admin',status=1)
 db.add_all([reviewer,admin]);db.commit()
 def publish(version):
  studio.test_agent_version(db,reviewer,version.id,{'issues':[]})
  approval=studio.submit_agent_version(db,reviewer,version.id,'probe')
  approval_service.decide_item(db,admin,approval.id,approve=True,note='probe')
  return db.query(CustomAgentRelease).filter_by(approval_id=approval.id).one()
 target,target_v1=studio.create_agent(db,reviewer,code='child_audit',name='child',description='',prompt='审查鉴权边界 V1',review_focus='鉴权边界 V1',model_config={})
 target_release_v1=publish(target_v1)
 _,skill_version=studio.create_skill(db,reviewer,code='root_delegate',name='delegation',description='',skill_type='agent_delegate',definition={'agent_code':target.code},requested_capabilities=[])
 root,root_v1=studio.create_agent(db,reviewer,code='root_audit',name='root',description='',prompt='根审查职责',review_focus='根审查',model_config={})
 studio.bind_skill(db,reviewer,root_v1.id,skill_version_id=skill_version.id,position=0,config={})
 root_release=publish(root_v1)
 first=F.resolve_release(db,root.code,release_id=root_release.id,version_id=root_v1.id,package_checksum=root_release.package_checksum,template_checksum=root_v1.checksum)
 target_v2=studio.revise_agent(db,reviewer,target.id,prompt='审查数据库事务 V2',review_focus='数据库事务 V2',model_config={},note='probe update')
 publish(target_v2)
 second=F.resolve_release(db,root.code,release_id=root_release.id,version_id=root_v1.id,package_checksum=root_release.package_checksum,template_checksum=root_v1.checksum)
 first_context=first.skill_context if first else ''
 second_context=second.skill_context if second else ''
 print(json.dumps({'scenario':'published_delegate_version_drift','parent_release_id':root_release.id,'frozen_target_release_id':target_release_v1.id,'context_changed':first_context!=second_context,'v1_prompt_present_after_update':'审查鉴权边界 V1' in second_context,'v2_prompt_leaked':'审查数据库事务 V2' in second_context,'explicit_execution_node':'平台将独立调用的冻结子 Agent' in second_context,'resolution_failed':second is None},ensure_ascii=False))



async def cumulative_storage():
 from app.services.agent_responses_service import DatabaseCheckpointStore
 from app.services.deepseek_responses_runtime import RunCheckpoint
 from app.models.agent_response_run import AgentResponseRun,AgentResponseTranscriptMessage
 from app.api.v1.agent_responses import _server_history_transcript
 db=db_new();store=DatabaseCheckpointStore(db,user_id=1,surface='user',session_key='audit_storage');history=[]
 for i in range(100):
  history.append({'role':'user','content':f'entry_{i}:'+('x'*10000)})
  await store.save(RunCheckpoint(run_id=f'archive_{i}',model='deepseek-v4-flash',transcript=list(history),tools=[],status='completed'))
 rows=db.query(AgentResponseRun).all();checkpoint_bytes=sum(len(r.checkpoint_json.encode()) for r in rows)
 ledger=db.query(AgentResponseTranscriptMessage).all();ledger_json_bytes=sum(len(r.message_json.encode()) for r in ledger);digest_bytes=sum(len(r.message_sha256.encode()) for r in ledger)
 ledger_bytes=ledger_json_bytes+digest_bytes;unique=len(json.dumps(history,ensure_ascii=False).encode());total=checkpoint_bytes+ledger_bytes
 started=time.monotonic();recovered=_server_history_transcript(db,user_id=1,surface='user',session_id='audit_storage');duration=time.monotonic()-started
 print(json.dumps({'scenario':'cumulative_checkpoints','runs':len(rows),'unique_transcript_bytes':unique,'checkpoint_bytes':checkpoint_bytes,'transcript_ledger_json_bytes':ledger_json_bytes,'message_digest_bytes':digest_bytes,'transcript_ledger_payload_bytes':ledger_bytes,'stored_payload_bytes':total,'storage_amplification':round(total/unique,2),'history_recovered_items':len(recovered),'history_read_seconds':round(duration,3)},ensure_ascii=False))


async def million_token_checkpoint():
 from app.models.agent_response_run import AgentResponseRun,AgentResponseTranscriptMessage
 from app.services.agent_responses_service import DatabaseCheckpointStore
 from app.services.deepseek_responses_runtime import RunCheckpoint,estimate_tokens
 from app.api.v1.agent_responses import _server_history_transcript
 db=db_new();store=DatabaseCheckpointStore(db,user_id=1,surface='user',session_key='audit_million_token');history=[]
 for i in range(1100):
  marker=' EARLY_FACT=only_current_account' if i==1 else ' MIDDLE_FACT=review_must_be_read_only' if i==550 else ' LATE_FACT=latest_correction_overrides_earlier_scope' if i==1050 else ''
  history.append({'role':'user' if i%2==0 else 'assistant','content':f'history_item_{i:04d}{marker} '+('x'*3990)})
 token_count=estimate_tokens(history)
 await store.save(RunCheckpoint(run_id='million_first',model='deepseek-v4-flash',transcript=history,tools=[],status='completed'))
 extended=history+[{'role':'user','content':'接着处理最新更正，完整保留此前要求。'}]
 await store.save(RunCheckpoint(run_id='million_second',model='deepseek-v4-flash',transcript=extended,tools=[]))
 rows=db.query(AgentResponseRun).all();ledger=db.query(AgentResponseTranscriptMessage).all()
 checkpoint_bytes=sum(len(row.checkpoint_json.encode()) for row in rows)
 ledger_json_bytes=sum(len(row.message_json.encode()) for row in ledger)
 digest_bytes=sum(len(row.message_sha256.encode()) for row in ledger)
 ledger_bytes=ledger_json_bytes+digest_bytes
 restored=await DatabaseCheckpointStore(db,user_id=1,surface='user',session_key='audit_million_token').load('million_second')
 recovered=_server_history_transcript(db,user_id=1,surface='user',session_id='audit_million_token')
 isolated=await DatabaseCheckpointStore(db,user_id=2,surface='user',session_key='audit_million_token').load('million_first') is None
 assert token_count>1_000_000 and restored is not None and restored.transcript==extended and recovered==extended and isolated
 assert len(ledger)==len(extended)
 print(json.dumps({'scenario':'million_token_transcript_roundtrip','estimated_tokens':token_count,'checkpoint_count':len(rows),'ledger_messages':len(ledger),'expected_messages':len(extended),'checkpoint_bytes':checkpoint_bytes,'transcript_ledger_json_bytes':ledger_json_bytes,'message_digest_bytes':digest_bytes,'transcript_ledger_payload_bytes':ledger_bytes,'storage_amplification':round((checkpoint_bytes+ledger_bytes)/len(json.dumps(extended,ensure_ascii=False).encode()),3),'ordered_recovery':recovered==extended,'early_anchor':'EARLY_FACT=only_current_account' in recovered[1]['content'],'middle_anchor':'MIDDLE_FACT=review_must_be_read_only' in recovered[550]['content'],'late_anchor':'LATE_FACT=latest_correction_overrides_earlier_scope' in recovered[1050]['content'],'account_isolation':isolated},ensure_ascii=False))



if __name__ == '__main__':
    high_retry()
    wave()
    asyncio.run(semantic())
    delegate_version_drift()
    asyncio.run(cumulative_storage())
    asyncio.run(million_token_checkpoint())
