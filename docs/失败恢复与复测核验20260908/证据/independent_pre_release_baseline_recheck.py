#!/usr/bin/env python3
"""只读重算已落盘 JSON；不连接生产、不运行被审采集器 main。"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy

repo = Path(__file__).resolve().parents[3]
current = repo / 'docs/失败恢复与复测核验20260908/证据'
previous = repo / 'docs/完整待办与漏洞知识更新20260907/证据'
paths = {
    'runtime_before': previous / '生产发布运行态与048核验.json',
    'runtime_current': current / '发布前生产版本与外键核验.json',
    'history_before': previous / '历史任务与孤儿账本发布后复核.json',
    'history_current': current / '发布前历史账本只读.json',
    'history_collector': previous / 'collect_historical_ledger.py',
    'runtime_collector': previous / 'collect_release_readonly.py',
}
read = lambda key: json.loads(paths[key].read_text())
old_runtime, runtime = read('runtime_before'), read('runtime_current')
old_history, history = read('history_before'), read('history_current')
checks = {}
checks['runtime_all_fields_except_observation_equal'] = (
    {k: v for k, v in runtime.items() if k != 'observed_at_utc'} ==
    {k: v for k, v in old_runtime.items() if k != 'observed_at_utc'})
tables = {'agent_response_run', 'agent_team', 'review_task', 'ai_call_log',
          'agent_mesh_message', 'pentest_engagement', 'sandbox_environment'}
targets = {'root_agent_run_id': 'agent_response_run', 'agent_run_id': 'agent_response_run',
           'tool_execution_id': 'agent_tool_execution', 'agent_team_id': 'agent_team',
           'agent_team_task_id': 'agent_team_task', 'agent_execution_event_id': 'agent_team_event'}
foreign_keys = runtime['foreign_keys']
checks['exact_42_unique_foreign_keys'] = len(foreign_keys) == 42 and {
    (row['table'], row['column']) for row in foreign_keys} == {(t, c) for t in tables for c in targets}
checks['foreign_key_contract'] = all(row['target_schema'] == 'code_review'
    and row['target'] == targets[row['column']] and row['target_column'] == 'id'
    and row['nullable'] is True and row['delete_rule'] == 'SET NULL'
    and row['source_type'] == 'bigint' and row['column_count'] == 1 for row in foreign_keys)
checks['runtime_readonly'] = runtime['business_writes'] is False and runtime['readonly_transaction']['read_only'] == 1
checks['release_3_8_6'] = runtime['version'] == '3.8.6' and runtime['expected_release'] == '0a14b5359a446ab7b447e10c061b1ebbe6d15e29'
checks['revision_048'] = runtime['alembic_revision'] == '048_ai_usage_attribution'
checks['frontend_summary_269_and_same_manifest'] = runtime['frontend_files'] == old_runtime['frontend_files'] and runtime['frontend_files']['expected_count'] == 269
stable_keys = ['historical_task', 'historical_team_tasks', 'historical_event', 'orphan_tool', 'unresolved_tool',
               'relationship', 'owner_mismatch', 'task_member_team_mismatch']
for key in stable_keys:
    checks[key + '_unchanged'] = history[key] == old_history[key]
checks['two_historical_tasks'] = [(x['task_id'], x['team_id'], x['task_status'], x['team_status']) for x in history['historical_task']] == [(11,5,'queued','failed'),(35,12,'waiting_dependency','failed')]
checks['seven_orphan_ids'] = [r['id'] for r in history['orphan_tool']] == [125,126,127,128,129,2580,2581]
checks['three_unresolved_ids'] = [(r['id'],r['status'],r['run_status']) for r in history['unresolved_tool']] == [(472,'executing','failed'),(634,'executing','failed'),(2286,'executing','failed')]
checks['history_readonly'] = history['business_writes'] is False and history['transaction'][0]['read_only'] == 1 and history['snapshot_end'][0]['read_only'] == 1
query = runpy.run_path(str(paths['history_collector']))['SQL']
checks['exact_query_sha256'] = hashlib.sha256(query.encode()).hexdigest() == history['query_sha256'] == old_history['query_sha256']
checks['query_starts_readonly_ends_rollback'] = 'SET SESSION TRANSACTION READ ONLY;' in query and 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;' in query and query.rstrip().endswith('ROLLBACK;')
counts = lambda data: {(r['table'],r['status']):r['count'] for r in data['status_counts']}
a, b = counts(old_history), counts(history)
differences = [{'table': key[0], 'status': key[1], 'before': a.get(key,0), 'current': b.get(key,0), 'delta': b.get(key,0)-a.get(key,0)} for key in sorted(a.keys()|b.keys()) if a.get(key,0) != b.get(key,0)]
checks['tool_counts_reconcile'] = sum(v for (table,_),v in b.items() if table == 'agent_tool_execution') == history['tool_integrity'][0]['total'] == 2608
checks['tool_integrity_valid'] = all(history['tool_integrity'][0][key] == 0 for key in ['invalid_arguments','terminal_missing_result','request_binding_mismatch'])
canonical_sha = lambda data: hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
report = {
    'scope': 'independent offline JSON recount and collector read; no production commands',
    'production_requests': 0, 'business_writes': False, 'passed': all(checks.values()), 'checks': checks,
    'source_sha256': {k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()},
    'foreign_key_recount': dict(sorted(Counter(r['table'] for r in foreign_keys).items())),
    'frontend_evidence_boundary': 'Current evidence retains count and canonical digest, not per-file maps. Compared current count/digest with prior verified runtime; no claim of independently recomputing current 269 raw file hashes.',
    'historical_rows': {k: {'count':len(history[k]),'canonical_sha256':canonical_sha(history[k])} for k in stable_keys},
    'dynamic_global_status_deltas': differences,
    'dynamic_count_boundary': 'Aggregate snapshot deltas do not identify callers or prove causes. Historical selected rows remained unchanged.',
}
output=current/'发布前版本与历史账本独立复核.json'
output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':report['passed'],'checks':checks,'dynamic_global_status_deltas':differences},ensure_ascii=False,indent=2))
raise SystemExit(0 if report['passed'] else 1)
