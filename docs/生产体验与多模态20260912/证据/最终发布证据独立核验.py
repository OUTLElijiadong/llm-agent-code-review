import hashlib,json,subprocess
from pathlib import Path
p=Path('docs/生产体验与多模态20260912/证据')
d=p/'最终发布只读.xrfyq93b'
load=lambda f:json.loads(f.read_text())
hashof=lambda f:hashlib.sha256(f.read_bytes()).hexdigest()
summary=load(d/'最终发布前后比对.json')
files=list(p.glob('*.json'))+list(d.glob('*.json'))
hashes={hashof(f):f for f in files}
inputs={k:hashes[v] for k,v in summary['input_sha256'].items()}
assert len(inputs)==7
before,after=load(inputs['ledger_before']),load(inputs['ledger_after'])
counts={}
for key in ['historical_task','historical_team_tasks','historical_event','orphan_tool','unresolved_tool']:
 assert before[key]==after[key],key
 counts[key]=len(after[key])
assert list(counts.values())==[2,6,23,7,3]
pb,pa=load(inputs['project_before']),load(inputs['project_after'])
assert pb['field_comparison']==pa['field_comparison']
assert len(pa['field_comparison'])==8
assert pb['row_sha256']==pa['row_sha256']==summary['project_comparison']['row_sha256']
assert all(x['equals_before_release'] for x in pa['field_comparison'])
rs,re=load(inputs['release_start']),load(inputs['release_end'])
assert {k:v for k,v in rs.items() if k!='observed_at_utc'}=={k:v for k,v in re.items() if k!='observed_at_utc'}
release=summary['expected_release']
assert rs['expected_release']==release
assert len(rs['containers'])==2
assert all(x['release']==release and x['health']=='healthy' and x['version']=='3.9.1' for x in rs['containers'])
assert len(rs['foreign_keys'])==42
assert len({(x['table'],x['column']) for x in rs['foreign_keys']})==42
assert all(x['delete_rule']=='SET NULL' and x['target_column']=='id' and x['column_count']==1 and x['target_schema']=='code_review' for x in rs['foreign_keys'])
for path,digest in rs['running_source_sha256'].items():
 raw=subprocess.check_output(['git','show',f'{release}:backend/{path}'])
 assert hashlib.sha256(raw).hexdigest()==digest,path
assert rs['frontend_files']['expected_count']==274 and rs['frontend_files']['all_expected_equal']
a=load(inputs['attribution'])
assert len(a['reference_contracts'])==42 and not a['anomalies']
assert {(x['table'],x['column']) for x in a['reference_contracts']}=={(x['table'],x['column']) for x in rs['foreign_keys']}
assert all(not r[k] for r in a['reference_contracts'] for k in ['missing_target','source_owner_unknown','target_owner_unknown','owner_mismatch'])
for r in a['reference_contracts']:
 assert 0<=r['populated']<=r['total_rows']
 assert r['total_rows']==a['source_attribution_fingerprints'][r['table']]['rows']
versions=load(d/'运行容器依赖版本.json')
assert versions['app_release']==release and versions['pip_check']['exit_code']==0
assert versions['versions']['cryptography']=='50.0.1' and versions['versions']['Pillow']=='12.3.0'
result={'status':'passed','inputs_sha256_verified':7,'historical_rows_equal':counts,'project_fields_equal':8,'project_row_sha256_equal':True,'release_identity_start_end_equal':True,'containers_healthy':2,'foreign_keys_verified':42,'running_source_files_match_git':5,'actual_reference_contracts_no_anomalies':42,'frontend_current_release_assets':274,'frontend_served_all_retained_assets':rs['frontend_files']['served_count'],'dependency_pip_check':0,'limits':['没有发布前42项实际引用快照，不能证明引用前后相等','274本版资源完整根据脱敏采集断言；本地无逐资源原始清单，不能独立重算manifest','历史行完整相等；项目八字段与整行哈希相等，脱敏哈希不能还原原始值','未核多模态实际调用与附件存储验收，不替代真实界面验收']}
print(json.dumps(result,ensure_ascii=False,indent=2))
