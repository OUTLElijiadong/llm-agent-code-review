"""仅核查用户复测所举任务162是否存在及父项目状态，不回传报告/源码。"""
import json
import subprocess
sql = """SET SESSION TRANSACTION READ ONLY;
START TRANSACTION READ ONLY;
SELECT JSON_OBJECT('kind','snapshot','read_only',@@transaction_read_only,'utc',UTC_TIMESTAMP(6));
SELECT JSON_OBJECT('kind','task','id',t.id,'status',t.status,'project_id',t.project_id,'project_exists',p.id IS NOT NULL,'project_status',p.status) FROM review_task t LEFT JOIN project p ON p.id=t.project_id WHERE t.id=162;
ROLLBACK;
"""
cmd = ['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c', 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database=code_review --default-character-set=utf8mb4 --batch --raw --skip-column-names']
p = subprocess.run(cmd, input=sql, text=True, capture_output=True, timeout=30)
assert p.returncode == 0, 'readonly_query_failed'
rows = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
assert rows[0]['read_only'] == 1
print(json.dumps({'business_writes':False, 'source':'production_readonly', 'rows':rows}, ensure_ascii=False, indent=2))
