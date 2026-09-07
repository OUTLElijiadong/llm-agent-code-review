"""本地发起生产只读事务；报告原文只在生产进程内解析，输出摘要与指纹。"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "backend/app/services/sandbox_report_summary.py"
SQL = """
SET SESSION MAX_EXECUTION_TIME=20000;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('read_only',@@transaction_read_only,'utc',UTC_TIMESTAMP(6),'task_id',t.id,'status',t.status,'stored_total',t.total_issues,'updated',t.update_time,'report_md',JSON_UNQUOTE(JSON_EXTRACT(r.content_json,'$.report_md'))) FROM review_task t JOIN review_report r ON r.task_id=t.id AND r.user_id=t.user_id WHERE t.id=161 AND t.review_type='sandbox_test';
ROLLBACK;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('审查任务161真实报告解析核验.json'))
    args = parser.parse_args()
    source = HELPER.read_text()
    remote = "import hashlib,json,subprocess\n"
    remote += f"source={source!r}\nexec(compile(source, 'sandbox_report_summary.py', 'exec'))\n"
    remote += f"sql={SQL!r}\n"
    remote += """
cmd=['docker','exec','-i','cr_mysql','sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database=code_review --default-character-set=utf8mb4 --batch --raw --skip-column-names']
p=subprocess.run(cmd,input=sql,text=True,capture_output=True,check=True,timeout=25)
row=json.loads(p.stdout)
md=row.pop('report_md')
row['report_md_sha256']=hashlib.sha256(md.encode()).hexdigest()
row['helper_sha256']=hashlib.sha256(source.encode()).hexdigest()
row['summary']=summarize_sandbox_report(md)
print(json.dumps(row,ensure_ascii=False))
"""
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', 'root@81.70.251.90', 'python3', '-'],
        input=remote, text=True, capture_output=True, timeout=45, check=True,
    )
    report = json.loads(result.stdout)
    assert report['read_only'] == 1 and report['task_id'] == 161
    assert report['helper_sha256'] == hashlib.sha256(HELPER.read_bytes()).hexdigest()
    report.update(
        collector_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        query_sha256=hashlib.sha256(SQL.encode()).hexdigest(),
        collected_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        business_writes=False, actual_model_http_sent=False,
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
