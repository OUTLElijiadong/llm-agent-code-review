"""生产只读账本核查；只输出标识、状态、关联与内容摘要，不输出原始会话。"""
import collections
import datetime
import hashlib
import json
import re
import subprocess


SQL = """
SET SESSION MAX_EXECUTION_TIME=20000;
SET SESSION time_zone='+00:00';
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('kind','transaction','utc',UTC_TIMESTAMP(6),'read_only',@@transaction_read_only,'isolation',@@transaction_isolation);
"""

for table in ('agent_response_run', 'agent_tool_execution', 'agent_team', 'agent_team_task', 'agent_team_member', 'agent_mesh_message', 'ops_execution'):
    SQL += f"SELECT JSON_OBJECT('kind','status_counts','table','{table}','status',status,'count',COUNT(*)) FROM {table} GROUP BY status;\n"

relations = [
    ('tool_run', 'agent_tool_execution c LEFT JOIN agent_response_run p ON p.run_id=c.run_id', 'p.id IS NULL'),
    ('tool_user', 'agent_tool_execution c LEFT JOIN user p ON p.id=c.user_id', 'p.id IS NULL'),
    ('run_user', 'agent_response_run c LEFT JOIN user p ON p.id=c.user_id', 'p.id IS NULL'),
    ('team_user', 'agent_team c LEFT JOIN user p ON p.id=c.user_id', 'p.id IS NULL'),
    ('task_team', 'agent_team_task c LEFT JOIN agent_team p ON p.id=c.team_id', 'p.id IS NULL'),
    ('task_member', 'agent_team_task c LEFT JOIN agent_team_member p ON p.id=c.member_id', 'p.id IS NULL'),
    ('member_team', 'agent_team_member c LEFT JOIN agent_team p ON p.id=c.team_id', 'p.id IS NULL'),
    ('event_team', 'agent_team_event c LEFT JOIN agent_team p ON p.id=c.team_id', 'p.id IS NULL'),
    ('event_task', 'agent_team_event c LEFT JOIN agent_team_task p ON p.id=c.task_id', 'c.task_id IS NOT NULL AND p.id IS NULL'),
    ('event_member', 'agent_team_event c LEFT JOIN agent_team_member p ON p.id=c.member_id', 'c.member_id IS NOT NULL AND p.id IS NULL'),
    ('mesh_event_message', 'agent_mesh_message_event c LEFT JOIN agent_mesh_message p ON p.message_id=c.message_id', 'p.id IS NULL'),
    ('run_mesh_message', 'agent_response_run c LEFT JOIN agent_mesh_message p ON p.message_id=c.mesh_message_id', 'c.mesh_message_id IS NOT NULL AND p.id IS NULL'),
]
for name, tables, predicate in relations:
    SQL += f"SELECT JSON_OBJECT('kind','relationship','name','{name}','missing',COUNT(*)) FROM {tables} WHERE {predicate};\n"

SQL += """
SELECT JSON_OBJECT('kind','tool_integrity','total',COUNT(*),'request_binding_mismatch',COALESCE(SUM(request_id<>SHA2(CONCAT('responses:',run_id,':',call_id),256)),0),'invalid_arguments',COALESCE(SUM(NOT JSON_VALID(arguments_json)),0),'terminal_missing_result',COALESCE(SUM(status IN ('success','failed') AND (result_json IS NULL OR NOT JSON_VALID(result_json))),0)) FROM agent_tool_execution;
SELECT JSON_OBJECT('kind','owner_mismatch','count',COUNT(*)) FROM agent_tool_execution t JOIN agent_response_run r ON r.run_id=t.run_id WHERE t.user_id<>r.user_id;
SELECT JSON_OBJECT('kind','task_member_team_mismatch','count',COUNT(*)) FROM agent_team_task t JOIN agent_team_member m ON m.id=t.member_id WHERE t.team_id<>m.team_id;
SELECT JSON_OBJECT('kind','historical_task','task_id',t.id,'team_id',t.team_id,'task_status',t.status,'team_status',g.status,'member_id',t.member_id,'member_status',m.status,'attempt_count',t.attempt_count,'dependency_keys',IF(JSON_VALID(t.dependency_keys_json),CAST(t.dependency_keys_json AS JSON),NULL),'lease_present',t.lease_token IS NOT NULL,'lease_expires_at',t.lease_expires_at,'started_at',t.started_at,'completed_at',t.completed_at,'created',t.create_time,'updated',t.update_time,'result_sha256',SHA2(t.result_json,256),'error_sha256',SHA2(t.errors_json,256),'team_error_sha256',SHA2(g.error_json,256),'team_completed_at',g.completed_at) FROM agent_team_task t LEFT JOIN agent_team g ON g.id=t.team_id LEFT JOIN agent_team_member m ON m.id=t.member_id WHERE t.id IN (11,35) OR (g.status IN ('completed','failed','cancelled') AND t.status NOT IN ('completed','failed','blocked','cancelled','dead_letter','expired')) ORDER BY t.id;
SELECT JSON_OBJECT('kind','historical_team_tasks','task_id',t.id,'team_id',t.team_id,'member_id',t.member_id,'status',t.status,'attempts',t.attempt_count,'created',t.create_time,'updated',t.update_time,'dependency_keys',IF(JSON_VALID(t.dependency_keys_json),CAST(t.dependency_keys_json AS JSON),NULL)) FROM agent_team_task t WHERE t.team_id IN (5,12) ORDER BY t.team_id,t.id;
SELECT JSON_OBJECT('kind','historical_event','id',id,'team_id',team_id,'task_id',task_id,'member_id',member_id,'event_type',event_type,'from_status',from_status,'to_status',to_status,'detail_sha256',SHA2(detail_json,256),'created',create_time) FROM agent_team_event WHERE team_id IN (5,12) ORDER BY id;
SELECT JSON_OBJECT('kind','orphan_tool','id',t.id,'run_sha256',SHA2(t.run_id,256),'user_id',t.user_id,'tool_name',t.tool_name,'status',t.status,'created',t.create_time,'updated',t.update_time,'arguments_valid',JSON_VALID(t.arguments_json),'result_valid',JSON_VALID(t.result_json),'result_status',JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(t.result_json),t.result_json,'{}'),'$.status')),'request_binding_matches',t.request_id=SHA2(CONCAT('responses:',t.run_id,':',t.call_id),256),'arguments_sha256',SHA2(t.arguments_json,256),'result_sha256',SHA2(t.result_json,256),'error_sha256',SHA2(t.error,256),'approval_count',(SELECT COUNT(*) FROM approval_item a WHERE a.copilot_request_id=t.request_id),'gateway_count',(SELECT COUNT(*) FROM tool_call_log c WHERE c.copilot_request_id=t.request_id),'ops_count',(SELECT COUNT(*) FROM ops_execution o WHERE o.request_id=t.request_id)) FROM agent_tool_execution t LEFT JOIN agent_response_run r ON r.run_id=t.run_id WHERE r.id IS NULL ORDER BY t.id;
SELECT JSON_OBJECT('kind','unresolved_tool','id',t.id,'run_db_id',r.id,'run_status',r.status,'tool_name',t.tool_name,'status',t.status,'created',t.create_time,'updated',t.update_time,'result_sha256',SHA2(t.result_json,256)) FROM agent_tool_execution t LEFT JOIN agent_response_run r ON r.run_id=t.run_id WHERE t.status='executing' ORDER BY t.id;
SELECT JSON_OBJECT('kind','snapshot_end','utc',UTC_TIMESTAMP(6),'read_only',@@transaction_read_only);
ROLLBACK;
"""


def main():
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    command = ['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c',
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot '
               '--database=code_review --default-character-set=utf8mb4 --connect-timeout=8 '
               '--batch --raw --skip-column-names']
    result = subprocess.run(command, input=SQL, capture_output=True, text=True, timeout=90)
    report = {'schema': 'historical-ledger-readonly-v1', 'started_at_utc': started,
              'query_sha256': hashlib.sha256(SQL.encode()).hexdigest(), 'business_writes': False}
    if result.returncode:
        report['error'] = {'returncode': result.returncode,
                           'codes': re.findall(r'ERROR (\d+)', result.stderr),
                           'lines': re.findall(r'at line (\d+)', result.stderr),
                           'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest()}
    else:
        groups = collections.defaultdict(list)
        for line in result.stdout.splitlines():
            row = json.loads(line)
            groups[row.pop('kind')].append(row)
        report.update(groups)
    report['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
