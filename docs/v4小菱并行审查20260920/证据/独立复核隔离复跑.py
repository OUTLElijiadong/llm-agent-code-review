import sys, json, contextlib, io, hashlib
from pathlib import Path
root=Path('/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台')
source=root/'docs/v4小菱并行审查20260920/证据/prism_v4_independent_review.py'
code=source.read_text().replace("project.status='quarantined';db.commit()", "db.add(ProjectMember(project_id=1,user_id=2,role_in_project='reviewer'));db.commit()\nout['member_restored_before_quarantine']=snapshot(member)\nproject.status='quarantined';db.commit()")
namespace={}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(code,str(source),'exec'),namespace)
from app.services.agent_team_summary import dependency_finding_summary, summarize_dependencies
from app.services.agent_team_review import _review_result
from app.services.agent_team_service import _public

db=namespace['db']; review=db.get(namespace['ReviewTask'],1)
review.status='success';review.processed_files=1;review.total_files=1;review.coverage={'stage':'complete'}
for i in range(100):
    db.add(namespace['ReviewIssue'](task_id=1,file_id=1,file_name='source.py',line_number=i+20,title=f'formal-{i}',description='合成复核数据',severity='高',issue_type='安全漏洞',status='unfixed'))
db.commit()
formal=_review_result(review,db=db)
audit={'status':'completed','findings':[{'project_id':1,'file_path':f'file-{i}.py','line_number':1,'title':f'audit-{i}','severity':'高'} for i in range(250)]}
def dep(result):
    return {'status':'completed','result':_public(result),'finding_summary':dependency_finding_summary(result,redact=_public)}
context={'formal':dep(formal),'audit':dep(audit)}
summarized=summarize_dependencies(context)
minimal=summarized['artifacts'][0]['data']
only_names={'status':'completed','evidence':[{'data':{'project_id':1,'findings':[{'file_name':name,'line_number':1,'title':'相同类型问题','severity':'高'} for name in ('one.py','two.py')]}}]}
file_names_summary=summarize_dependencies({'audit':dep(only_names)})['artifacts'][0]['data']
result={'source_script_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'isolation':'sqlite in-memory; restored membership before quarantine; no network/model calls','baseline_recheck':namespace['out'],'bounded_summary':{'formal_total':formal['finding_count_total'],'formal_returned':len(formal['findings']),'formal_truncated':formal['findings_truncated'],'audit_total':250,'audit_returned':len(context['audit']['finding_summary']['items']),'audit_omitted':context['audit']['finding_summary']['omitted_count'],'audit_source_truncated':context['audit']['finding_summary']['source_truncated'],'summary_count':minimal['unique_finding_count'],'bounded_tasks':minimal['bounded_finding_tasks'],'human_summary':summarized['summary'],'scope':minimal['scope'],'summary_omitted_counts':minimal.get('omitted_counts')},'filename_only_dedup':{'input_file_names':['one.py','two.py'],'result_count':file_names_summary['unique_finding_count'],'result':file_names_summary['findings']}}
out=root/'docs/v4小菱并行审查20260920/证据/独立复核复跑.json'
out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
print(json.dumps({'bounded_summary':result['bounded_summary'],'filename_only_dedup':result['filename_only_dedup']},ensure_ascii=False,indent=2))
