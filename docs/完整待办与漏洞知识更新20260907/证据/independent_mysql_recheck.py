"""静态独立重算MySQL迁移证据，不导入或执行数据库采集器。"""
import ast
import collections
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def assignment(path, name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def main():
    evidence = HERE / "MySQL迁移核验.json"
    collector = HERE / "verify_usage_migration_mysql.py"
    migration = ROOT / "backend/alembic/versions/048_ai_usage_attribution.py"
    data = json.loads(evidence.read_text())
    required_runtime_sources = {
        'backend/app/services/ai_usage_context.py', 'backend/app/models/usage_attribution.py',
        'backend/app/models/agent_response_run.py', 'backend/app/models/agent_team.py',
        'backend/app/models/review_task.py', 'backend/app/models/ai_call_log.py',
        'backend/app/models/agent_mesh.py', 'backend/app/models/pentest.py',
        'backend/app/models/agent_capability.py',
    }
    expected_sources = required_runtime_sources | {
        'backend/alembic/versions/048_ai_usage_attribution.py', 'backend/alembic/env.py',
        'backend/app/core/config.py', 'backend/app/core/database.py',
        'deploy/mysql/init.sql', 'deploy/mysql/seed.sql',
    }
    assert set(data['source_sha256_before']) == set(assignment(collector, 'SOURCE_FILES')) == expected_sources
    assert set(data['runtime_source_sha256']) == required_runtime_sources
    assert data['source_sha256_after'] == data['source_sha256_before']
    assert data['source_unchanged_during_run'] is True
    for name, digest in data['source_sha256_after'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    for name, digest in data['runtime_source_sha256'].items():
        assert data['source_sha256_before'][name] == digest, name
    assert hashlib.sha256(collector.read_bytes()).hexdigest() == data['collector_sha256']
    tables = assignment(migration, "_TABLES")
    fields = assignment(migration, "_FIELDS")
    assert set(assignment(collector, "TABLES")) == set(tables)
    assert set(assignment(collector, "FIELDS")) == set(fields)
    actual = {(row["table"], row["field"], row["target"], row["ondelete"]) for row in data["foreign_keys"]}
    expected = {(table, field, target, "SET NULL") for table in tables for field, target in fields.items()}
    assert len(actual) == len(data["foreign_keys"]) == data["foreign_key_count"] == 42
    assert actual == expected
    assert len({row["name"] for row in data["foreign_keys"]}) == 42
    assert all(row["name"] == f"fk_{row['table']}_{row['field']}" for row in data["foreign_keys"])
    assert data["status"] == "passed" and data["environment"] == "local-isolated-mysql"
    assert data["production_connected"] is False
    assert data["audit_commit_survives_business_rollback"] is True
    assert data["audit_does_not_commit_pending_business_user"] is True
    assert data["actual_model_http_sent"] is False
    flags = ("orphan_fk_rejected", "delete_preserves_log_and_nulls_fk", "legacy_fields_unchanged",
             "historical_attribution_null", "old_orphan_executing_preserved", "downgrade_and_reupgrade",
             "isolated_database_removed")
    assert all(data[field] is True for field in flags)
    source = collector.read_text()
    assert "host='127.0.0.1', port=3307" in source
    assert "DB_HOST='127.0.0.1', DB_PORT='3307'" in source
    assert "strip_new(after) == before" in source
    assert "status='executing', run_id='local-orphan-048'" in source
    assert "DROP DATABASE `{database}`" in source
    assert "business.flush()" in source and "business.rollback()" in source
    assert "independent.get(User, 987655) is None" in source
    assert "(row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 7, 7)" in source
    assert "record_usage_attempt(model_name='local-provider-fixture'" in source
    # 当前seed没有跳转数据库；采集器的SQL门禁只包住init，后续维护需保持这一事实。
    seed = (ROOT / "deploy/mysql/seed.sql").read_text()
    import re
    assert not re.search(r"\b(?:USE|CREATE DATABASE|DROP DATABASE)\b", seed, re.I)
    output = {"status": "passed", "production_connections": 0, "database_connections": 0,
              "tested_database": data['database'],
              "tested_started_at_utc": data['started_at_utc'],
              "tested_finished_at_utc": data['finished_at_utc'],
              "runtime_source_files_verified": len(required_runtime_sources),
              "all_source_files_verified": len(expected_sources),
              "source_unchanged_during_run_and_matches_checkout": True,
              "foreign_key_count": len(actual),
              "foreign_keys_per_table": dict(collections.Counter(row["table"] for row in data["foreign_keys"])),
              "foreign_key_targets_match_migration": True,
              "isolation": "本机127.0.0.1:3307随机prism_acceptance_048库；采集器明确传递DB环境给alembic；独立复核未重连。",
              "preservation_scope": "7张来源表及工具表的合成本地夹具；048新增归因全NULL，旧字段保持，旧executing孤儿保持；不是生产历史全库比较。",
              "fk_behavior_scope": "42约束结构全部检查；实际错误FK拒绝及父删除SET NULL行为在ai_call_log.root_agent_run_id上代表性验证。",
              "migration_roundtrip": "047→048→047→048，降级后旧schema精确恢复，原字段比较保持；本地测试删除的根行属于验证夹具。",
              "independent_audit_transaction": "business先flush合成User987655，直接调用record_usage_attempt写0/7/7 tokens后rollback；另建Session确认用量在而业务User不存在，证明独立事务未暗中提交业务。",
              "provider_http": "未发送；本次7 tokens是明确标记的合成输入，只测试真实MySQL事务，不作真实模型用量声明。",
              "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (evidence, collector, migration, ROOT / "backend/alembic/env.py",
                                             ROOT / "backend/app/core/config.py", ROOT / "deploy/mysql/seed.sql",
                                             ROOT / "backend/app/services/ai_usage_context.py")}}
    (HERE / "独立MySQL迁移复核.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "foreign_keys": 42, "database_connections": 0}))


if __name__ == "__main__":
    main()
