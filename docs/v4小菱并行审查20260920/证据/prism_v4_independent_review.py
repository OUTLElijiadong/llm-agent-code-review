import sys
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, '/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend')
sys.path.insert(0, '/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/backend/tests')
import conftest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.user import User
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Role, Permission, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.review_issue import ReviewIssue
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_task_file import ReviewTaskFile
from app.api.v1.review import router as review_router
from app.api.v1.reports import router as reports_router
from app.services import review_service, report_service, issue_service
from app.services.review_input_service import freeze_task_inputs
from app.ai.multi_agent import get_agent_profiles

engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
sessions = sessionmaker(bind=engine, expire_on_commit=False)
db = sessions()
owner = User(id=1, username='owner', password='isolated', role='user', status=1)
member = User(id=2, username='member', password='isolated', role='user', status=1)
role = Role(id=1,name='普通用户',code='user',status='active',is_builtin=1)
db.add_all([owner,member,role])
for idx, code in enumerate(('review:view','issue:view','report:view','report:export:json'),1):
    db.add(Permission(id=idx,code=code,name=code,module=code.split(':')[0],type='api'))
    db.add(RolePermission(role_id=1,permission_id=idx))
db.add_all([UserRole(user_id=1,role_id=1),UserRole(user_id=2,role_id=1)])
project = Project(id=1,user_id=1,project_name='private-project',status='active',language='python')
task = ReviewTask(id=1,user_id=2,project_id=1,task_name='member-task',status='success',total_files=1,processed_files=1)
file = CodeFile(id=1,project_id=1,file_name='source.py',file_path='source.py',language='python',content='secret = 1\n',version_no=1,line_count=1,status='active')
db.add_all([project,task,file,CodeVersion(file_id=1,version_no=1,content=file.content,create_time=datetime.now(timezone.utc)),ReviewIssue(id=1,task_id=1,file_id=1,file_name='source.py',title='secret issue',description='private code evidence',severity='高',issue_type='安全漏洞',status='unfixed')])
db.add(ProjectMember(project_id=1,user_id=2,role_in_project='reviewer'))
db.commit()
freeze_task_inputs(db,1,[file]); db.commit()
app=FastAPI(); register_handlers(app)
app.include_router(review_router,prefix='/api/review'); app.include_router(reports_router,prefix='/api/reports')
app.dependency_overrides[get_db] = lambda: db
client=TestClient(app,raise_server_exceptions=False)
def snapshot(user):
    headers={'Authorization':'Bearer '+create_access_token(user.id,user.role,user.token_version or 0)}
    result={}
    for path in ('/api/review/tasks/1','/api/reports/1','/api/reports/tasks/1/export?format=json','/api/reports'):
        response=client.get(path,headers=headers)
        result[path]={'status':response.status_code,'data':response.json().get('data') if path=='/api/reports' else None}
    return result
out={'owner_while_member_present':snapshot(owner),'member_present':snapshot(member)}
db.query(ProjectMember).filter_by(project_id=1,user_id=2).delete();db.commit()
out['member_removed']=snapshot(member)
project.status='quarantined';db.commit()
out['project_quarantined_member']=snapshot(member)
project.status='active';db.commit()
file.file_name='renamed.ts';file.language='typescript';db.commit()
out['report_snapshot']={'task_files':review_service._task_file_summaries(db,1),'report_files':report_service._build_file_summaries(db,1)}

# An old review worker receives a superseded lease signal during file review.
task.status='running';task.execution_token='old';task.processed_files=0;task.coverage={'stage':'queued'};task.start_time=datetime.now(timezone.utc);db.commit()
def lose_lease(*args,**kwargs):
    with sessions() as other:
        current=other.get(ReviewTask,1)
        current.execution_token='new';current.coverage={'stage':'analyzing','new_worker_marker':True,'files':{'1':{'status':'complete','completed_chunks':99}}};other.commit()
    raise review_service.TaskSupersededError('old worker should not write')
with patch.object(review_service,'_review_one_file',lose_lease),patch.object(review_service,'_emit_review_event'):
    review_service._execute_review(db,None,None,task,member,[file],[],get_agent_profiles('standard'),'',execution_token='old')
db.expire_all();current=db.get(ReviewTask,1)
out['superseded_worker']={'execution_token':current.execution_token,'coverage':current.coverage,'status':current.status}
print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
