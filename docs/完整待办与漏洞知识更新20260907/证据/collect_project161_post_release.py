"""发布后仅重读项目161，按已核验的原始规范化算法输出摘要；不重读/恢复旧备份。"""
import argparse
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

COLUMNS = [('id', 'bigint'), ('user_id', 'bigint'), ('project_name', 'varchar(100)'),
           ('description', 'varchar(500)'), ('language', 'varchar(50)'), ('status', 'varchar(20)'),
           ('create_time', 'datetime'), ('update_time', 'datetime')]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    assert re.fullmatch('[a-f0-9]{40}', args.expected_release)
    release = subprocess.check_output(['git', '-C', '/opt/code-review', 'rev-parse', 'HEAD'], text=True).strip()
    assert release == args.expected_release
    baseline = json.loads(args.baseline.read_text())
    assert baseline['status'] == '通过' and baseline['all_eight_fields_equal']
    values = ','.join("'"+name+"',"+("DATE_FORMAT(`"+name+"`,'%Y-%m-%d %H:%i:%s')" if typ == 'datetime' else '`'+name+'`') for name, typ in COLUMNS)
    sql = '''SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('kind','schema','name',COLUMN_NAME,'type',COLUMN_TYPE,'nullable',IS_NULLABLE='YES','charset',CHARACTER_SET_NAME,'key',COLUMN_KEY,'precision',DATETIME_PRECISION)
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='project' ORDER BY ORDINAL_POSITION;
SELECT JSON_OBJECT('kind','count','rows',COUNT(*),'target_rows',SUM(id=161),'readonly',@@transaction_read_only,'observed_at_utc',UTC_TIMESTAMP(6)) FROM project;
'''+"SELECT JSON_OBJECT('kind','target',"+values+") FROM project WHERE id=161;\nROLLBACK;"
    result = subprocess.run(['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c',
                             'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database=code_review --default-character-set=utf8mb4 --batch --raw --skip-column-names'],
                            input=sql, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, 'readonly query failed'
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    schema = [r for r in records if r['kind'] == 'schema']
    assert [(r['name'], r['type']) for r in schema] == COLUMNS
    assert [r['name'] for r in schema if r['key'] == 'PRI'] == ['id']
    original_schema = baseline['parser']['complete_columns']
    assert all(bool(r['nullable']) == old['nullable'] for r, old in zip(schema, original_schema))
    assert all(r['precision'] == 0 for r in schema if r['type'] == 'datetime')
    assert all(r['charset'] == ('utf8mb4' if r['type'].startswith('varchar') else None) for r in schema)
    target, = [r for r in records if r['kind'] == 'target']
    counts, = [r for r in records if r['kind'] == 'count']
    assert counts['target_rows'] == counts['readonly'] == 1
    normalized = []
    for name, typ in COLUMNS:
        value = target[name]
        if value is None:
            assert name in ('description', 'language')
        elif typ == 'bigint':
            assert type(value) is int and -(2**63) <= value < 2**63
            value = str(value)
        elif typ == 'datetime':
            assert isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value)
            datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        else:
            assert isinstance(value, str) and len(value) <= int(re.search(r'\d+', typ).group())
        normalized.append([name, typ, value])
    previous = {r['field']: r['current_normalized_sha256'] for r in baseline['field_comparison']}
    fields = [{'field': r[0], 'sha256': digest(r), 'equals_before_release': digest(r) == previous[r[0]]} for r in normalized]
    row_hash = digest(normalized)
    equals = all(r['equals_before_release'] for r in fields) and row_hash == baseline['row_fingerprint']['current_normalized_sha256']
    print(json.dumps({'status': 'passed' if equals else 'failed', 'release': release,
                      'current': counts, 'schema_equal': True, 'field_comparison': fields,
                      'row_sha256': row_hash, 'all_eight_fields_equal': equals,
                      'baseline_file_sha256': hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                      'backup_reread': False, 'sql_write_executed': False}, ensure_ascii=False, indent=2))
    assert equals, 'project161 fields changed'


if __name__ == '__main__':
    main()
