"""Opt-in backup/restore proof using two marker-verified disposable MySQL schemas."""

import os
import re
import secrets
import subprocess

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


def _require_marker_only_target(engine):
    if (inspect(engine).get_table_names() != ['_prism_platform_cost_test_identity']
            or inspect(engine).get_view_names()):
        raise RuntimeError('refusing restore database that is not exclusively marker-initialized')


def test_mysql_owned_runs_backup_restore_guards():
    source_raw = os.environ.get('PRISM_PLATFORM_COST_TEST_MYSQL_URL', '')
    target_raw = os.environ.get('PRISM_PLATFORM_COST_RESTORE_MYSQL_URL', '')
    source_nonce = os.environ.get('PRISM_PLATFORM_COST_TEST_NONCE', '')
    target_nonce = os.environ.get('PRISM_PLATFORM_COST_RESTORE_NONCE', '')
    container_id = os.environ.get('PRISM_PLATFORM_COST_TEST_CONTAINER_ID', '')
    if not source_raw or not target_raw:
        pytest.skip('requires marker-verified disposable source and restore schemas')
    source = make_url(source_raw)
    target = make_url(target_raw)
    if (any(url.get_backend_name() != 'mysql' or url.host != '127.0.0.1'
            or not re.fullmatch(r'prism_platform_cost_test_[0-9a-f]{10}', url.database or '')
            for url in (source, target))
            or source.database == target.database or source.port != target.port
            or source.username != target.username or source.password != target.password
            or not re.fullmatch(r'[0-9a-f]{64}', container_id)
            or not re.fullmatch(r'[0-9a-f]{64}', source_nonce)
            or not re.fullmatch(r'[0-9a-f]{64}', target_nonce)):
        pytest.fail('refusing non-disposable backup/restore target')
    identity = subprocess.run(
        ['docker', 'inspect', '--format', '{{.Id}} {{index .Config.Labels "prism.task"}} {{.State.Running}}',
         container_id], capture_output=True, text=True, check=False, timeout=5,
    )
    port = subprocess.run(['docker', 'port', container_id, '3306/tcp'], capture_output=True,
                          text=True, check=False, timeout=5)
    if (identity.returncode != 0 or identity.stdout.strip() != f'{container_id} platform-cost-isolated true'
            or port.returncode != 0 or port.stdout.strip() != f'127.0.0.1:{source.port}'):
        pytest.fail('refusing unverified MySQL container or port')
    source_engine = create_engine(source)
    target_engine = create_engine(target)
    tables = ('agent_response_run', 'platform_cost_center', 'platform_cost_budget', 'platform_cost_attempt',
              'platform_cost_run_owner', 'platform_cost_attempt_owner')
    snapshots = (
        'SELECT id,run_id,user_id,surface,session_key,status,checkpoint_json,version '
        'FROM agent_response_run ORDER BY id',
        'SELECT id,run_pk,actor_id,generation,owner_digest,status FROM platform_cost_run_owner ORDER BY id',
        'SELECT id,attempt_id,run_owner_id,generation,owner_digest FROM platform_cost_attempt_owner ORDER BY id',
        'SELECT id,budget_id,actor_id,attempt_digest,payload_digest,status,reserved_cny_micros,pricing_version '
        'FROM platform_cost_attempt ORDER BY id',
    )
    try:
        for engine, nonce in ((source_engine, source_nonce), (target_engine, target_nonce)):
            with engine.connect() as connection:
                values = connection.execute(
                    text('SELECT nonce FROM _prism_platform_cost_test_identity')
                ).scalars().all()
            if values != [nonce]:
                pytest.fail('refusing database with mismatched identity marker')
        _require_marker_only_target(target_engine)
        with source_engine.connect() as connection:
            before = tuple(connection.execute(text(f'SELECT COUNT(*) FROM {name}')).scalar_one() for name in tables)
            snapshot_before = tuple(connection.execute(text(query)).all() for query in snapshots)
        assert before[-2] > 0 and before[-1] > 0
        # Credentials only enter a 0600 file in this disposable container via
        # stdin; neither command arguments nor the backup bytes are printed.
        authfile = '/tmp/prism-restore-' + secrets.token_hex(12) + '.cnf'
        auth = f'[client]\nuser={source.username}\npassword={source.password}\n'.encode()
        subprocess.run(['docker', 'exec', '-i', container_id, 'sh', '-c', f'umask 077; cat > {authfile}'],
                       input=auth, capture_output=True, check=True, timeout=10)
        dump = subprocess.run(
            ['docker', 'exec', container_id, 'mysqldump', f'--defaults-extra-file={authfile}',
             '--single-transaction', '--triggers', '--set-gtid-purged=OFF', '--skip-add-drop-table',
             source.database, *tables], capture_output=True, check=True, timeout=30,
        ).stdout
        subprocess.run(['docker', 'exec', '-i', container_id, 'mysql', f'--defaults-extra-file={authfile}',
                        target.database], input=dump, capture_output=True, check=True, timeout=30)
        with target_engine.connect() as connection:
            after = tuple(connection.execute(text(f'SELECT COUNT(*) FROM {name}')).scalar_one() for name in tables)
            assert after == before
            assert tuple(connection.execute(text(query)).all() for query in snapshots) == snapshot_before
            run_pk = connection.execute(text('SELECT run_pk FROM platform_cost_run_owner ORDER BY id LIMIT 1'))\
                .scalar_one()
            expected = {'pc_run_owner_no_delete', 'pc_run_owner_identity', 'pc_run_identity', 'pc_run_no_delete',
                        'pc_run_owner_insert', 'pc_attempt_owner_no_delete', 'pc_attempt_owner_immutable',
                        'pc_attempt_identity', 'pc_attempt_no_delete', 'pc_attempt_owner_insert'}
            present = set(connection.execute(text(
                'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()'
            )).scalars())
            assert expected <= present
            foreign_keys = inspect(connection).get_foreign_keys('platform_cost_run_owner')
            assert any(fk['constrained_columns'] == ['run_pk'] and fk['referred_table'] == 'agent_response_run'
                       and fk['referred_columns'] == ['id']
                       and fk['options'].get('ondelete', '').upper() == 'RESTRICT' for fk in foreign_keys)
            for sql in ('DELETE FROM agent_response_run WHERE id=:pk',
                        'DELETE FROM platform_cost_run_owner WHERE run_pk=:pk',
                        'UPDATE agent_response_run SET user_id=user_id+1 WHERE id=:pk'):
                with pytest.raises(DBAPIError):
                    connection.execute(text(sql), {'pk': run_pk})
                connection.rollback()
        with pytest.raises(RuntimeError, match='refusing restore database'):
            _require_marker_only_target(target_engine)
        # An isolated DML-only principal tests the boundary without assuming the
        # production application's actual grants. The container is disposable.
        username = 'prism_dml_' + secrets.token_hex(6)
        password = secrets.token_urlsafe(24)
        with target_engine.begin() as admin:
            admin.execute(text(f"CREATE USER '{username}'@'%' IDENTIFIED BY :password"), {'password': password})
            admin.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{target.database}`.* TO '{username}'@'%'"))
        restricted = create_engine(target.set(username=username, password=password))
        try:
            with restricted.connect() as connection:
                try:
                    connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=0')
                    with pytest.raises(DBAPIError):
                        connection.execute(text('DELETE FROM agent_response_run WHERE id=:pk'), {'pk': run_pk})
                    with pytest.raises(DBAPIError):
                        connection.exec_driver_sql('DROP TRIGGER pc_run_no_delete')
                finally:
                    connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=1')
                    connection.rollback()
        finally:
            restricted.dispose()
    finally:
        source_engine.dispose()
        target_engine.dispose()
