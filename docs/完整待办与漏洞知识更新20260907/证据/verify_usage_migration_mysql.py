"""在本机专用临时MySQL库验证048；不读写code_review业务行，不连接生产。"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
TABLES = ('agent_response_run', 'agent_team', 'review_task', 'ai_call_log',
          'agent_mesh_message', 'pentest_engagement', 'sandbox_environment')
FIELDS = ('root_agent_run_id', 'agent_run_id', 'tool_execution_id', 'agent_team_id',
          'agent_team_task_id', 'agent_execution_event_id')

SOURCE_FILES = (
    'backend/app/services/ai_usage_context.py',
    'backend/alembic/versions/048_ai_usage_attribution.py',
    'backend/app/models/usage_attribution.py',
    'backend/app/models/agent_response_run.py',
    'backend/app/models/agent_team.py',
    'backend/app/models/review_task.py',
    'backend/app/models/ai_call_log.py',
    'backend/app/models/agent_mesh.py',
    'backend/app/models/pentest.py',
    'backend/app/models/agent_capability.py',
    'backend/alembic/env.py',
    'backend/app/core/config.py',
    'backend/app/core/database.py',
    'deploy/mysql/init.sql',
    'deploy/mysql/seed.sql',
)


def source_fingerprints():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def main():
    info = json.loads(subprocess.check_output(['docker', 'inspect', 'cr_mysql'], text=True))[0]
    secrets = dict(value.split('=', 1) for value in info['Config']['Env'] if '=' in value)
    password = secrets['MYSQL_ROOT_PASSWORD']
    assert info['NetworkSettings']['Ports']['3306/tcp'][0] == {'HostIp': '127.0.0.1', 'HostPort': '3307'}
    database = 'prism_acceptance_048_' + uuid.uuid4().hex[:12]
    assert re.fullmatch(r'prism_acceptance_048_[a-f0-9]{12}', database)
    admin = create_engine(URL.create('mysql+pymysql', username='root', password=password,
                                    host='127.0.0.1', port=3307))
    engine = create_engine(URL.create('mysql+pymysql', username='root', password=password,
                                     host='127.0.0.1', port=3307, database=database))
    before_sources = source_fingerprints()
    report = {'database': database, 'environment': 'local-isolated-mysql',
              'source_sha256_before': before_sources,
              'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'production_connected': False, 'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    env = dict(os.environ, APP_ENV='dev', DB_HOST='127.0.0.1', DB_PORT='3307',
               DB_USER='root', DB_PASSWORD=password, DB_NAME=database, INITIAL_ADMIN_PASSWORD=uuid.uuid4().hex)

    def migrate(target, direction='upgrade'):
        completed = subprocess.run([str(ROOT/'backend/.venv/bin/alembic'), direction, target],
                                   cwd=ROOT/'backend', env=env, capture_output=True, text=True, timeout=180)
        safe = re.sub(r'input_value=.*?input_type=dict', 'input_value=[REDACTED], input_type=dict', (completed.stdout + completed.stderr).replace(password, '[REDACTED]'))
        (OUT/f'mysql-{direction}-{target}.log').write_text(safe)
        if completed.returncode:
            raise RuntimeError(f'{direction} {target} failed; see redacted log')

    def snapshot():
        with engine.connect() as conn:
            return {table: [dict(row) for row in conn.execute(text(f'SELECT * FROM `{table}`')).mappings()]
                    for table in TABLES + ('agent_tool_execution',)}

    def strip_new(rows):
        return {table: [{key: value for key, value in row.items() if key not in FIELDS} for row in values]
                for table, values in rows.items()}

    with admin.begin() as conn:
        conn.execute(text(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'))
    try:
        sql = (ROOT/'deploy/mysql/init.sql').read_text()
        sql, count = re.subn(r'CREATE DATABASE IF NOT EXISTS code_review\s+DEFAULT CHARACTER SET utf8mb4\s+DEFAULT COLLATE utf8mb4_unicode_ci;\s+USE code_review;', '', sql)
        assert count == 1 and not re.search(r'\b(?:USE|CREATE DATABASE|DROP DATABASE)\b', sql, re.I)
        command = ['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c',
                   'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database="$1"',
                   'mysql-fixture', database]
        sql += '\n' + (ROOT/'deploy/mysql/seed.sql').read_text()
        assert not re.search(r'\b(?:USE|CREATE DATABASE|DROP DATABASE)\b', sql, re.I)
        completed = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, 'isolated baseline init failed'
        migrate('047_review_input_snapshot')
        before_schema = {table: [column['name'] for column in inspect(engine).get_columns(table)] for table in TABLES}
        # 每张来源表一条明确标注的本地夹具；无任何真实用户记录复制。
        with engine.begin() as conn:
            for table in TABLES + ('agent_tool_execution',):
                values = {'id': 987654}
                for column in inspect(engine).get_columns(table):
                    if column['name'] == 'id' or column['nullable'] or column['default'] is not None:
                        continue
                    typ = str(column['type']).lower()
                    name = column['name']
                    if any(t in typ for t in ('int', 'decimal', 'float', 'double')):
                        values[name] = 1
                    elif 'date' in typ or 'time' in typ:
                        values[name] = datetime.datetime(2026, 1, 1)
                    else:
                        values[name] = '{}' if name.endswith('_json') else 'local-fixture'
                if table == 'agent_tool_execution':
                    values.update(status='executing', run_id='local-orphan-048',
                                  call_id='local-call', request_id=hashlib.sha256(b'responses:local-orphan-048:local-call').hexdigest())
                names = ','.join('`'+name+'`' for name in values)
                params = ','.join(':'+name for name in values)
                conn.execute(text(f'INSERT INTO `{table}` ({names}) VALUES ({params})'), values)
        before = snapshot()
        migrate('048_ai_usage_attribution')
        after = snapshot()
        assert strip_new(after) == before, 'legacy fields were changed'
        assert all(row[field] is None for table in TABLES for row in after[table] for field in FIELDS)
        constraints = []
        for table in TABLES:
            assert set(FIELDS) <= {column['name'] for column in inspect(engine).get_columns(table)}
            for fk in inspect(engine).get_foreign_keys(table):
                if fk['constrained_columns'][0] in FIELDS:
                    assert fk['options'].get('ondelete') == 'SET NULL'
                    constraints.append({'table': table, 'name': fk['name'], 'field': fk['constrained_columns'][0],
                                        'target': fk['referred_table'], 'ondelete': fk['options']['ondelete']})
        assert len(constraints) == len(TABLES)*len(FIELDS)
        # 真实MySQL的独立连接：业务回滚不能撤销已经消耗的模型用量。
        sys.path.insert(0, str(ROOT/'backend'))
        for key in ('APP_ENV', 'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'INITIAL_ADMIN_PASSWORD'):
            os.environ[key] = env[key]
        from app.models import load_all_models
        load_all_models()
        from app.models.ai_call_log import AiCallLog
        from app.models.user import User
        from app.services.ai_usage_context import record_usage_attempt, usage_context
        runtime_modules = {}
        for name in SOURCE_FILES:
            if name.startswith('backend/app/models/') or name == 'backend/app/services/ai_usage_context.py':
                module_name = name.removeprefix('backend/').removesuffix('.py').replace('/', '.')
                loaded_path = Path(sys.modules[module_name].__file__).resolve()
                assert loaded_path == (ROOT/name).resolve(), 'runtime module resolved outside current checkout'
                runtime_modules[name] = hashlib.sha256(loaded_path.read_bytes()).hexdigest()
        assert all(before_sources[name] == digest for name, digest in runtime_modules.items())
        report['runtime_source_sha256'] = runtime_modules
        with Session(engine) as business:
            pending = User(id=987655, username='local-usage-rollback', password='local-fixture', role='user', status=1)
            business.add(pending)
            business.flush()
            with usage_context(1, {'root_agent_run_id': 987654, 'agent_run_id': 987654}, db=business):
                log_id = record_usage_attempt(model_name='local-provider-fixture', agent_label='local-acceptance',
                                              status='success', usage={'prompt_tokens': 0, 'completion_tokens': 7, 'total_tokens': 7})
            business.rollback()
        with Session(engine) as independent:
            row = independent.get(AiCallLog, log_id)
            assert row is not None and row.user_id == 1 and row.root_agent_run_id == 987654
            assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 7, 7)
            assert independent.get(User, 987655) is None
        report['audit_commit_survives_business_rollback'] = True
        report['audit_does_not_commit_pending_business_user'] = True
        report['actual_model_http_sent'] = False
        try:
            with engine.begin() as conn:
                conn.execute(text('UPDATE ai_call_log SET root_agent_run_id=999999999 WHERE id=987654'))
        except IntegrityError:
            report['orphan_fk_rejected'] = True
        else:
            raise AssertionError('orphan FK accepted')
        with engine.begin() as conn:
            conn.execute(text('UPDATE ai_call_log SET root_agent_run_id=987654 WHERE id=987654'))
            conn.execute(text('DELETE FROM agent_response_run WHERE id=987654'))
            assert conn.execute(text('SELECT root_agent_run_id FROM ai_call_log WHERE id=987654')).scalar() is None
            assert conn.execute(text('SELECT COUNT(*) FROM ai_call_log WHERE id=987654')).scalar() == 1
        report['delete_preserves_log_and_nulls_fk'] = True
        pre_down = strip_new(snapshot())
        migrate('047_review_input_snapshot', 'downgrade')
        assert snapshot() == pre_down
        assert {table: [column['name'] for column in inspect(engine).get_columns(table)] for table in TABLES} == before_schema
        migrate('048_ai_usage_attribution')
        assert strip_new(snapshot()) == pre_down
        report['source_sha256_after'] = source_fingerprints()
        assert report['source_sha256_after'] == before_sources, 'source files changed during isolated acceptance'
        report['source_unchanged_during_run'] = True
        report.update(status='passed', foreign_keys=constraints, foreign_key_count=len(constraints),
                      legacy_fields_unchanged=True, historical_attribution_null=True,
                      old_orphan_executing_preserved=True, downgrade_and_reupgrade=True)
    except Exception as exc:
        report.update(status='failed', error=str(exc).replace(password, '[REDACTED]'))
        raise
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP DATABASE `{database}`'))
            report['isolated_database_removed'] = conn.execute(text('SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name'), {'name': database}).scalar() == 0
        admin.dispose()
        report['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (OUT/'MySQL迁移核验.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({key: value for key, value in report.items() if key != 'foreign_keys'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
