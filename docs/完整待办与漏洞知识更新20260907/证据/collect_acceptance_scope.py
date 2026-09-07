"""只读核对本轮四个专用账号是否产生模型调用或伪造审查；不输出业务正文。"""
import json
from datetime import datetime, timezone

from sqlalchemy import text
from app.core.database import SessionLocal

ACCOUNTS = {103: 'qa_20260907_owner_a', 104: 'qa_20260907_member_a',
            105: 'qa_20260907_owner_b', 106: 'qa_20260907_no_permission'}
with SessionLocal() as db:
    rows = db.execute(text('SELECT id,username,status FROM user WHERE id IN (103,104,105,106)')).mappings().all()
    assert {r['id']: r['username'] for r in rows} == ACCOUNTS
    report = {'captured_at_utc': datetime.now(timezone.utc).isoformat(), 'account_ids': list(ACCOUNTS),
              'accounts': [dict(row) for row in rows], 'read_only': True, 'counts': {}}
    for table in ('ai_call_log', 'review_task', 'project'):
        report['counts'][table] = db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE user_id IN (103,104,105,106)')).scalar_one()
    report['file_ids'] = list(db.execute(text('SELECT id FROM code_file WHERE project_id IN (162,163) ORDER BY id')).scalars())
    db.rollback()
print(json.dumps(report, ensure_ascii=False, indent=2))
