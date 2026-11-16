"""Test-only DDL guards for the detached platform-cost metadata.

These are not a migration and must not be treated as installed in production.
Fresh test schema creation also installs triggers on the existing run table.
Automatic teardown is deliberately blocked: ownership and partial-DDL recovery
need a reviewed migration, not a blind DROP TRIGGER. DDL privileges remain an
explicit deployment boundary. Keep creation and restore tests for both DBs.
"""

from sqlalchemy import DDL, event, inspect


def register_guards(run_owner, attempt_owner):
    names = {
        'pc_run_owner_no_delete', 'pc_run_owner_identity', 'pc_run_identity', 'pc_run_no_delete',
        'pc_run_no_replace', 'pc_run_owner_insert', 'pc_attempt_owner_no_delete',
        'pc_attempt_owner_immutable', 'pc_attempt_identity', 'pc_attempt_no_delete', 'pc_attempt_no_replace',
        'pc_attempt_owner_insert',
    }

    @event.listens_for(run_owner.metadata, 'before_create')
    def refuse_existing_schema(_metadata, connection, **_kwargs):
        if set(inspect(connection).get_table_names()) & set(run_owner.metadata.tables):
            raise RuntimeError('平台成本实验表已存在，禁止自动重建或修复')
        if connection.dialect.name == 'sqlite':
            rows = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.dialect.name == 'mysql':
            rows = connection.exec_driver_sql(
                'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()'
            )
        else:
            raise RuntimeError('平台成本实验数据库类型不受支持')
        if names & set(rows.scalars()):
            raise RuntimeError('已有触发器与平台成本实验冲突，禁止自动安装')

    @event.listens_for(run_owner.metadata, 'before_drop')
    def refuse_unsafe_teardown(_metadata, _connection, **_kwargs):
        raise RuntimeError('禁止自动删除平台成本实验表；需审查触发器归属和恢复方案')

    immutable_attempt = (
        'id', 'budget_id', 'actor_id', 'attempt_digest', 'attempt_bytes', 'payload_digest', 'model_name',
        'provider_name', 'provider_endpoint_host', 'credential_source', 'pricing_version',
        'input_cost_cny_micros_per_million', 'output_cost_cny_micros_per_million',
        'input_limit', 'output_limit', 'reserved_cny_micros', 'created_at',
    )
    sqlite_attempt_change = ' OR '.join(f'NEW.{name} IS NOT OLD.{name}' for name in immutable_attempt)
    mysql_attempt_change = ' OR '.join(
        f'NOT (BINARY NEW.{name} <=> BINARY OLD.{name})' for name in immutable_attempt
    )
    sqlite = (
        (run_owner, """CREATE TRIGGER pc_run_owner_no_delete BEFORE DELETE ON platform_cost_run_owner
          BEGIN SELECT RAISE(ABORT, 'platform run owner is retained'); END"""),
        (run_owner, """CREATE TRIGGER pc_run_owner_identity BEFORE UPDATE ON platform_cost_run_owner
          WHEN NEW.id IS NOT OLD.id OR NEW.run_pk IS NOT OLD.run_pk OR NEW.actor_id IS NOT OLD.actor_id
            OR NEW.owner_digest IS NOT OLD.owner_digest OR NEW.generation < OLD.generation
            OR (OLD.status = 'cancelled' AND NEW.status != 'cancelled')
          BEGIN SELECT RAISE(ABORT, 'platform run owner identity is immutable'); END"""),
        (run_owner, """CREATE TRIGGER pc_run_identity BEFORE UPDATE ON agent_response_run
          WHEN EXISTS (SELECT 1 FROM platform_cost_run_owner o WHERE o.run_pk = OLD.id)
            AND (NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.surface IS NOT OLD.surface
              OR NEW.session_key IS NOT OLD.session_key OR NEW.run_id IS NOT OLD.run_id)
          BEGIN SELECT RAISE(ABORT, 'owned run identity is immutable'); END"""),
        (run_owner, """CREATE TRIGGER pc_run_no_delete BEFORE DELETE ON agent_response_run
          WHEN EXISTS (SELECT 1 FROM platform_cost_run_owner o WHERE o.run_pk = OLD.id)
          BEGIN SELECT RAISE(ABORT, 'owned run is retained'); END"""),
        (run_owner, """CREATE TRIGGER pc_run_no_replace BEFORE INSERT ON agent_response_run
          WHEN EXISTS (SELECT 1 FROM agent_response_run r JOIN platform_cost_run_owner o ON o.run_pk = r.id
                       WHERE r.id = NEW.id OR r.run_id = NEW.run_id)
          BEGIN SELECT RAISE(ABORT, 'owned run cannot be replaced'); END"""),
        (run_owner, """CREATE TRIGGER pc_run_owner_insert BEFORE INSERT ON platform_cost_run_owner
          WHEN EXISTS (SELECT 1 FROM platform_cost_run_owner o WHERE o.id = NEW.id OR o.run_pk = NEW.run_pk)
            OR NOT EXISTS (SELECT 1 FROM agent_response_run r WHERE r.id = NEW.run_pk
                           AND r.user_id = NEW.actor_id AND r.surface = 'admin')
          BEGIN SELECT RAISE(ABORT, 'platform run owner scope mismatch'); END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_no_delete BEFORE DELETE ON platform_cost_attempt_owner
          BEGIN SELECT RAISE(ABORT, 'platform attempt owner is retained'); END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_immutable BEFORE UPDATE ON platform_cost_attempt_owner
          BEGIN SELECT RAISE(ABORT, 'platform attempt owner is immutable'); END"""),
        (attempt_owner, f"""CREATE TRIGGER pc_attempt_identity BEFORE UPDATE ON platform_cost_attempt
          WHEN EXISTS (SELECT 1 FROM platform_cost_attempt_owner o WHERE o.attempt_id = OLD.id)
            AND ({sqlite_attempt_change} OR (OLD.status = 'unknown' AND NEW.status != 'unknown'))
          BEGIN SELECT RAISE(ABORT, 'owned attempt is immutable'); END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_no_delete BEFORE DELETE ON platform_cost_attempt
          WHEN EXISTS (SELECT 1 FROM platform_cost_attempt_owner o WHERE o.attempt_id = OLD.id)
          BEGIN SELECT RAISE(ABORT, 'owned attempt is retained'); END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_no_replace BEFORE INSERT ON platform_cost_attempt
          WHEN EXISTS (SELECT 1 FROM platform_cost_attempt a JOIN platform_cost_attempt_owner o ON o.attempt_id = a.id
                       WHERE a.id = NEW.id OR a.attempt_digest = NEW.attempt_digest)
          BEGIN SELECT RAISE(ABORT, 'owned attempt cannot be replaced'); END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_insert BEFORE INSERT ON platform_cost_attempt_owner
          WHEN EXISTS (SELECT 1 FROM platform_cost_attempt_owner existing_owner WHERE existing_owner.id = NEW.id
                       OR existing_owner.attempt_id = NEW.attempt_id)
            OR NOT EXISTS (SELECT 1 FROM platform_cost_attempt a JOIN platform_cost_run_owner o
                           ON o.id = NEW.run_owner_id WHERE a.id = NEW.attempt_id
                           AND a.actor_id = o.actor_id AND o.generation = NEW.generation AND o.status = 'active')
          BEGIN SELECT RAISE(ABORT, 'platform attempt owner scope mismatch'); END"""),
    )
    mysql = (
        (run_owner, """CREATE TRIGGER pc_run_owner_no_delete BEFORE DELETE ON platform_cost_run_owner
          FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform run owner is retained'"""),
        (run_owner, """CREATE TRIGGER pc_run_owner_identity BEFORE UPDATE ON platform_cost_run_owner FOR EACH ROW
          BEGIN IF NOT (NEW.id <=> OLD.id) OR NOT (NEW.run_pk <=> OLD.run_pk)
            OR NOT (NEW.actor_id <=> OLD.actor_id) OR NOT (NEW.owner_digest <=> OLD.owner_digest)
            OR NEW.generation < OLD.generation OR (OLD.status = 'cancelled' AND NEW.status != 'cancelled') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform run owner identity is immutable'; END IF; END"""),
        (run_owner, """CREATE TRIGGER pc_run_identity BEFORE UPDATE ON agent_response_run FOR EACH ROW
          BEGIN IF EXISTS (SELECT 1 FROM platform_cost_run_owner o WHERE o.run_pk = OLD.id)
            AND (NOT (NEW.id <=> OLD.id) OR NOT (NEW.user_id <=> OLD.user_id)
              OR NOT (BINARY NEW.surface <=> BINARY OLD.surface)
              OR NOT (BINARY NEW.session_key <=> BINARY OLD.session_key)
              OR NOT (BINARY NEW.run_id <=> BINARY OLD.run_id)) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'owned run identity is immutable'; END IF; END"""),
        (run_owner, """CREATE TRIGGER pc_run_no_delete BEFORE DELETE ON agent_response_run FOR EACH ROW
          BEGIN IF EXISTS (SELECT 1 FROM platform_cost_run_owner o WHERE o.run_pk = OLD.id) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'owned run is retained'; END IF; END"""),
        (run_owner, """CREATE TRIGGER pc_run_owner_insert BEFORE INSERT ON platform_cost_run_owner FOR EACH ROW
          BEGIN IF NOT EXISTS (SELECT 1 FROM agent_response_run r WHERE r.id = NEW.run_pk
            AND r.user_id = NEW.actor_id AND BINARY r.surface = BINARY 'admin') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform run owner scope mismatch'; END IF; END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_no_delete BEFORE DELETE ON platform_cost_attempt_owner
          FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform attempt owner is retained'"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_immutable BEFORE UPDATE ON platform_cost_attempt_owner
          FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform attempt owner is immutable'"""),
        (attempt_owner, f"""CREATE TRIGGER pc_attempt_identity BEFORE UPDATE ON platform_cost_attempt FOR EACH ROW
          BEGIN IF EXISTS (SELECT 1 FROM platform_cost_attempt_owner o WHERE o.attempt_id = OLD.id)
            AND ({mysql_attempt_change} OR (OLD.status = 'unknown' AND NEW.status != 'unknown')) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'owned attempt is immutable'; END IF; END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_no_delete BEFORE DELETE ON platform_cost_attempt FOR EACH ROW
          BEGIN IF EXISTS (SELECT 1 FROM platform_cost_attempt_owner o WHERE o.attempt_id = OLD.id) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'owned attempt is retained'; END IF; END"""),
        (attempt_owner, """CREATE TRIGGER pc_attempt_owner_insert BEFORE INSERT ON platform_cost_attempt_owner
          FOR EACH ROW BEGIN IF NOT EXISTS (SELECT 1 FROM platform_cost_attempt a JOIN platform_cost_run_owner o
            ON o.id = NEW.run_owner_id WHERE a.id = NEW.attempt_id AND a.actor_id = o.actor_id
            AND o.generation = NEW.generation AND o.status = 'active') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'platform attempt owner scope mismatch'; END IF; END"""),
    )
    for dialect, guards in (('sqlite', sqlite), ('mysql', mysql)):
        for table, sql in guards:
            event.listen(table, 'after_create', DDL(sql).execute_if(dialect=dialect))
