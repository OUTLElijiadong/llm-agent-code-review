#!/usr/bin/env python3
"""离线核对项目161八字段摘要、报告161摘要和既有节点证据；不读取生产原文。"""
import ast
import hashlib
import json
from pathlib import Path
import runpy

repo=Path(__file__).resolve().parents[3]
old=repo/'docs/完整待办与漏洞知识更新20260907/证据'
new=repo/'docs/失败恢复与复测核验20260908/证据'
paths={
 'project_baseline':old/'项目161完整字段比较.json',
 'project_last':old/'项目161发布后完整字段复核.json',
 'project_current':new/'发布前项目161八字段核验.json',
 'project_collector':new/'collect_project161_fingerprint.py',
 'review_last':old/'审查任务161发布后解析核验.json',
 'review_current':new/'发布前审查任务161报告核验.json',
 'review_collector':old/'collect_review161_summary_readonly.py',
 'outline':old/'审查任务161报告结构只读复现.json',
 'helper':repo/'backend/app/services/sandbox_report_summary.py',
}
load=lambda key:json.loads(paths[key].read_text())
pb,pl,pc=map(load,['project_baseline','project_last','project_current'])
rl,rc,outline=map(load,['review_last','review_current','outline'])
checks={}
expected_fields=['id','user_id','project_name','description','language','status','create_time','update_time']
checks['exact_eight_fields']= [r['field'] for r in pc['field_comparison']]==expected_fields
checks['all_eight_hashes_equal_previous']=pc['field_comparison']==pl['field_comparison']
checks['all_eight_hashes_equal_backup_baseline']=all(row['sha256']==prior['current_normalized_sha256']==prior['backup_normalized_sha256'] for row,prior in zip(pc['field_comparison'],pb['field_comparison']))
checks['row_hash_equal']=pc['row_sha256']==pl['row_sha256']==pb['row_fingerprint']['backup_normalized_sha256']==pb['row_fingerprint']['current_normalized_sha256']
checks['baseline_file_sha_matches']=pc['baseline_file_sha256']==hashlib.sha256(paths['project_baseline'].read_bytes()).hexdigest()
# 独立解出collector常量，核对其嵌入基线确为已归档字节，而不执行main/SQL。
tree=ast.parse(paths['project_collector'].read_text())
embedded=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='fromhex' and isinstance(n.args[0],ast.Constant)]
checks['embedded_baseline_exact_bytes']=len(embedded)==1 and bytes.fromhex(embedded[0])==paths['project_baseline'].read_bytes()
checks['project_readonly_single_target']=pc['sql_write_executed'] is False and pc['backup_reread'] is False and pc['current']['readonly']==1 and pc['current']['target_rows']==1
checks['review_all_non_time_fields_equal']={k:v for k,v in rc.items() if k not in ('utc','collected_at_utc')}=={k:v for k,v in rl.items() if k not in ('utc','collected_at_utc')}
checks['review_helper_exact_sha']=rc['helper_sha256']==hashlib.sha256(paths['helper'].read_bytes()).hexdigest()
checks['review_collector_exact_sha']=rc['collector_sha256']==hashlib.sha256(paths['review_collector'].read_bytes()).hexdigest()
checks['review_query_exact_sha']=rc['query_sha256']==hashlib.sha256(runpy.run_path(str(paths['review_collector']))['SQL'].encode()).hexdigest()
checks['stored_16_preserved']=rc['stored_total']==16 and rc['status']=='success' and rc['updated']=='2026-08-19 02:23:00.000000'
headings=[r for r in outline['outline'] if r['node']=='heading' and r['field_label'] is None]
fields=[r for r in outline['outline'] if r['node']=='bullet' and r['field_label']]
checks['outline_4_findings_16_field_bullets']=len(headings)==4 and len(fields)==16
checks['no_explicit_severity_labels']=all(not row['severity_labels'] for row in outline['outline'])
checks['four_unclassified_summary']=rc['summary']['total']==rc['summary']['unclassified']==4 and sum(rc['summary']['severity_counts'].values())==0
checks['review_readonly_no_model']=rc['read_only']==1 and rc['business_writes'] is False and rc['actual_model_http_sent'] is False
report={'scope':'offline evidence/source recount; production requests zero; raw report not read', 'production_requests':0,
 'passed':all(checks.values()), 'checks':checks,'source_sha256':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()},
 'project_row_sha256':pc['row_sha256'],'report_md_sha256':rc['report_md_sha256'],
 'summary':rc['summary'],'boundary':'4 report findings are unclassified, not 4 confirmed vulnerabilities or zero risk. Current report hash/helper match prior evidence; old typed outline is recounted, raw current Markdown is not downloaded or reprocessed locally.'}
(new/'发布前项目与审查161独立复核.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':report['passed'],'checks':checks},ensure_ascii=False,indent=2))
raise SystemExit(0 if report['passed'] else 1)
