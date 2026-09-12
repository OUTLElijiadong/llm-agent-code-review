"""42 项归因外键的实际引用和所属用户核验；只读一致快照，不猜补历史 NULL。"""
import argparse
import collections
import datetime
import hashlib
import json
import re
import subprocess

TABLES = ('agent_response_run', 'agent_team', 'review_task', 'ai_call_log',
          'agent_mesh_message', 'pentest_engagement', 'sandbox_environment')
TARGETS = {'root_agent_run_id': 'agent_response_run', 'agent_run_id': 'agent_response_run',
           'tool_execution_id': 'agent_tool_execution', 'agent_team_id': 'agent_team',
           'agent_team_task_id': 'agent_team_task', 'agent_execution_event_id': 'agent_team_event'}
SQL = """SET SESSION MAX_EXECUTION_TIME=20000;
SET SESSION time_zone='+00:00';
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('kind','transaction','schema',DATABASE(),'utc',UTC_TIMESTAMP(6),'read_only',@@transaction_read_only,'isolation',@@transaction_isolation);
"""
for table in TABLES:
    owner = 'owner_id' if table == 'sandbox_environment' else 'user_id'
    for column, target in TARGETS.items():
        join = f'LEFT JOIN {target} p ON p.id=c.{column}'
        target_owner = 'p.user_id'
        if target == 'agent_team_task':
            join += ' LEFT JOIN agent_team g ON g.id=p.team_id'
            target_owner = 'g.user_id'
        SQL += f"""SELECT JSON_OBJECT('kind','reference','table','{table}','column','{column}',
'total_rows',COUNT(*),'populated',COUNT(c.{column}),
'missing_target',COALESCE(SUM(c.{column} IS NOT NULL AND p.id IS NULL),0),
'source_owner_unknown',COALESCE(SUM(c.{column} IS NOT NULL AND c.{owner} IS NULL),0),
'target_owner_unknown',COALESCE(SUM(p.id IS NOT NULL AND {target_owner} IS NULL),0),
'owner_mismatch',COALESCE(SUM(p.id IS NOT NULL AND c.{owner} IS NOT NULL AND {target_owner} IS NOT NULL AND c.{owner}<>{target_owner}),0))
FROM {table} c {join};
"""
    fields = ','.join(f"'{column}',{column}" for column in TARGETS)
    SQL += (f"SELECT JSON_OBJECT('kind','attribution_row','table','{table}','id',id,"
            f"'owner_id',{owner},{fields}) FROM {table} ORDER BY id;\n")
SQL += "SELECT JSON_OBJECT('kind','snapshot_end','utc',UTC_TIMESTAMP(6),'read_only',@@transaction_read_only);\nROLLBACK;"


def summarize(records):
    groups = collections.defaultdict(list)
    for row in records:
        groups[row['kind']].append(row)
    start, = groups['transaction']
    end, = groups['snapshot_end']
    if not (start['schema'] == 'code_review' and start['read_only'] == end['read_only'] == 1
            and start['isolation'] == 'REPEATABLE-READ'):
        raise ValueError('readonly_snapshot_contract')
    rows = groups['reference']
    if len(rows) != 42 or {(r['table'], r['column']) for r in rows} != {(t, c) for t in TABLES for c in TARGETS}:
        raise ValueError('reference_cardinality')
    fingerprints = {}
    for table in TABLES:
        entries = sorted((r for r in groups['attribution_row'] if r['table'] == table), key=lambda r: r['id'])
        if len(entries) != len({r['id'] for r in entries}):
            raise ValueError('duplicate_source_row')
        if any(r['total_rows'] != len(entries) for r in rows if r['table'] == table):
            raise ValueError('source_row_count_mismatch')
        raw = json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        fingerprints[table] = {'rows': len(entries), 'sha256': hashlib.sha256(raw).hexdigest()}
    anomalies = [r for r in rows if any(r[k] for k in
                 ('missing_target', 'source_owner_unknown', 'target_owner_unknown', 'owner_mismatch'))]
    return {'status': 'failed' if anomalies else 'passed', 'transaction': start, 'snapshot_end': end,
            'reference_contracts': rows, 'anomalies': anomalies, 'source_attribution_fingerprints': fingerprints,
            'scope': '42项非NULL引用的存在性与owner相等；team_task的owner经team解析，sandbox使用owner_id',
            'limits': '历史NULL只计未填不猜补；引用存在和owner相同不证明所有真实调用链语义，须结合执行验收'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-release', required=True)
    args = parser.parse_args()
    report = {'business_writes': False, 'query_sha256': hashlib.sha256(SQL.encode()).hexdigest(),
              'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        if not re.fullmatch('[a-f0-9]{40}', args.expected_release):
            raise ValueError('invalid_expected_release')
        git = ['git', '-C', '/opt/code-review', 'rev-parse', 'HEAD']
        if subprocess.check_output(git, text=True, stderr=subprocess.DEVNULL).strip() != args.expected_release:
            raise ValueError('release_mismatch_before')
        command = ['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c',
                   'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot '
                   '--database=code_review --default-character-set=utf8mb4 --connect-timeout=8 '
                   '--batch --raw --skip-column-names']
        result = subprocess.run(command, input=SQL, capture_output=True, text=True, timeout=90)
        if result.returncode:
            report['mysql_error_codes'] = re.findall(r'ERROR (\d+)', result.stderr)
            raise RuntimeError('readonly_query_failed')
        report.update(summarize([json.loads(line) for line in result.stdout.splitlines() if line.strip()]))
        if subprocess.check_output(git, text=True, stderr=subprocess.DEVNULL).strip() != args.expected_release:
            raise ValueError('release_mismatch_after')
        report['expected_release'] = args.expected_release
    except Exception as error:
        report.update(status='failed', failure_type=type(error).__name__)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
