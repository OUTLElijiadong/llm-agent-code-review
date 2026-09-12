"""独立重算已采集的脱敏生产证据；不连接数据库，不执行 collector.main。"""
import ast
import collections
import hashlib
import json
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent

def read(path):
    return json.loads(path.read_text())

ledger = read(EVIDENCE / '历史账本生产只读复核.json')
old = read(ROOT / 'docs/失败恢复与复测核验20260908/证据/发布后历史账本只读.json')
collector = ROOT / 'docs/完整待办与漏洞知识更新20260907/证据/collect_historical_ledger.py'
sql = runpy.run_path(str(collector))['SQL']
assert hashlib.sha256(sql.encode()).hexdigest() == ledger['query_sha256']
assert 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;' in sql
assert sql.rstrip().endswith('ROLLBACK;')
assert ledger['business_writes'] is False
assert ledger['transaction'][0]['read_only'] == ledger['snapshot_end'][0]['read_only'] == 1
assert ledger['transaction'][0]['isolation'] == 'REPEATABLE-READ'
counts = collections.Counter()
for row in ledger['status_counts']:
    assert isinstance(row['count'], int) and row['count'] >= 0
    counts[row['table']] += row['count']
assert counts['agent_tool_execution'] == ledger['tool_integrity'][0]['total']
relations = {row['name']: row['missing'] for row in ledger['relationship']}
assert relations['tool_run'] == len(ledger['orphan_tool'])
assert len({row['id'] for row in ledger['orphan_tool']}) == len(ledger['orphan_tool'])
unchanged = {}
for key in ('historical_task', 'historical_team_tasks', 'historical_event', 'orphan_tool', 'unresolved_tool'):
    unchanged[key] = {'rows': len(ledger[key]), 'equals_20260908_after_release': ledger[key] == old[key]}
    assert unchanged[key]['equals_20260908_after_release']
assert {(r['task_id'], r['team_id']) for r in ledger['historical_task']} == {(11, 5), (35, 12)}
assert sum(r['count'] for r in ledger['status_counts'] if r['table'] == 'agent_tool_execution' and r['status'] == 'executing') == len(ledger['unresolved_tool'])

project = read(EVIDENCE / '项目161发布前字段证据.json')
source = ROOT / 'docs/失败恢复与复测核验20260908/证据/collect_project161_fingerprint.py'
tree = ast.parse(source.read_text())
hex_literal = next(node.value.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'baseline_bytes' for target in node.targets))
baseline_bytes = bytes.fromhex(hex_literal)
baseline = json.loads(baseline_bytes)
assert hashlib.sha256(baseline_bytes).hexdigest() == project['baseline_file_sha256']
expected = {row['field']: row['backup_normalized_sha256'] for row in baseline['field_comparison']}
actual = {row['field']: row['sha256'] for row in project['field_comparison']}
assert len(project['field_comparison']) == len(actual) == 8 and actual == expected
assert project['row_sha256'] == baseline['row_fingerprint']['backup_normalized_sha256']
assert project['current']['target_rows'] == project['current']['readonly'] == 1
assert all(row['equals_before_release'] for row in project['field_comparison'])
assert project['all_eight_fields_equal'] and project['schema_equal']
assert project['sql_write_executed'] is False and project['backup_reread'] is False

production = read(EVIDENCE / '生产基线.json')
commit_count = int(subprocess.check_output(['git', 'rev-list', '--count', production['running_release'] + '..' + production['source_revision']], cwd=ROOT, text=True))
changed_files = subprocess.check_output(['git', 'diff', '--name-only', production['running_release'], production['source_revision']], cwd=ROOT, text=True).splitlines()
assert commit_count == 1 and all(name.startswith('deploy/') for name in changed_files)
summary = {
    '复核范围': '只读重算采集文件、核查采集SQL与本地Git对象；未独立再次连接生产数据库',
    '状态': '通过',
    '账本查询摘要匹配': True,
    '账本统计总数': dict(counts),
    '历史对象逐字段比较': unchanged,
    '缺失关联': relations,
    '项目161字段数': 8,
    '项目161旧基线摘要匹配': True,
    '项目161本轮发布后证据': '尚未采集，本文件只证明发布前重核',
    '源码领先运行提交数': commit_count,
    '源码领先涉及文件': changed_files,
    '输入文件摘要': {name: hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest() for name in ('生产基线.json', '历史账本生产只读复核.json', '项目161发布前字段证据.json')},
}
(EVIDENCE / '生产证据独立复核摘要.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2))
