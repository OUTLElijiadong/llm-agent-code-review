"""Regression tests for 121 schema and reviewed-reader release gates."""
from __future__ import annotations

import ast
import copy
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
import pytest  # noqa: E402

from billing_evidence_compatibility import EXPECTED_MODULES as FINGERPRINTS  # noqa: E402
from billing_evidence_compatibility import probe  # noqa: E402
from billing_evidence_schema_compatibility import EXPECTED, classify  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def complete_schema():
    return {
        'table_count': 1,
        'columns': [{'name': name, 'type': 'bigint', 'nullable': 'YES', 'default': None, 'extra': ''}
                    for name in ('model_search_calls_evidence', 'model_fetch_calls_evidence')],
        'checks': [{'enforced': 'YES', 'clause': EXPECTED}],
    }


def test_real_mysql_normalized_check_is_accepted():
    schema = complete_schema()
    schema['checks'][0]['clause'] = (
        "(((`model_search_calls_evidence` is null) and (`model_fetch_calls_evidence` is null)) or "
        "((`model_search_calls_evidence` is not null) and (`model_fetch_calls_evidence` is not null) and "
        "(`model_search_calls_evidence` = 0) and (`model_fetch_calls_evidence` = 0) and "
        "(`decision` in (_latin1'proposed',_latin1'approved',_latin1'rejected')) "
        "and (`resolution` = _latin1'settle')))"
    )
    assert classify(schema) == 'complete'
    schema['checks'][0]['clause'] = schema['checks'][0]['clause'].replace('_latin1', '_utf8mb4')
    assert classify(schema) == 'complete'
    assert classify({'table_count': 1, 'columns': [], 'checks': []}) == 'absent'


def test_schema_rejects_partial_unsafe_or_unenforced_constraints():
    for mutate in (
        lambda schema: schema['columns'].pop(),
        lambda schema: schema['columns'][0].update({'type': 'int'}),
        lambda schema: schema['columns'][0].update({'nullable': 'NO'}),
        lambda schema: schema['checks'][0].update({'enforced': 'NO'}),
        lambda schema: schema['checks'][0].update({'clause': EXPECTED.replace(' = 0', ' >= 0')}),
        lambda schema: schema['checks'][0].update({'clause': EXPECTED.replace(' AND resolution', ' OR resolution')}),
        lambda schema: schema.update({'table_count': 0}),
    ):
        schema = copy.deepcopy(complete_schema())
        mutate(schema)
        assert classify(schema) == 'partial'


def test_reader_requires_reviewed_modules_and_rejects_old_or_altered_code(tmp_path):
    app = ROOT / 'backend/app'
    assert probe(app)
    for relative in FINGERPRINTS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / relative, target)
    assert probe(tmp_path)
    path = tmp_path / 'services/billing_reservation_reconciliation_service.py'
    source = path.read_text(encoding='utf-8')
    assert '((None, None), (0, 0))' in source
    path.write_text(source.replace('((None, None), (0, 0))', '((None, None), (1, 0))', 1), encoding='utf-8')
    assert not probe(tmp_path)
    path.unlink()
    try:
        assert not probe(tmp_path)
    except FileNotFoundError:
        pass  # The CLI catches errors and reports unsupported.


@pytest.mark.parametrize('relative,function', [
    ('services/billing_reservation_reconciliation_service.py', '_fenced_team_model_attempt'),
    ('services/billing_reservation_reconciliation_service.py', 'recover_stale_team_model_attempt'),
    ('services/billing_reservation_reconciliation_service.py', '_require_complete_hold'),
    ('services/billing_ledger_service.py', 'release_verified_not_sent'),
    ('services/billing_ledger_service.py', '_release_not_sent'),
    ('services/billing_ledger_service.py', 'mark_unknown'),
    ('services/billing_ledger_service.py', 'settle'),
    ('services/enterprise_agent_team_runtime_service.py', '_lock_assistant_control_runs'),
    ('services/enterprise_agent_team_runtime_service.py', 'retry_task'),
    ('services/enterprise_agent_team_runtime_service.py', 'cancel_team'),
    ('services/enterprise_agent_team_runtime_service.py', '_check_lease'),
    ('services/enterprise_agent_team_runtime_service.py', '_require_approved_plan'),
    ('services/enterprise_agent_team_runtime_service.py', '_provider_transaction_read_committed'),
    ('services/enterprise_agent_team_dispatcher.py', '_task_billing_scope'),
    ('services/enterprise_assistant_runtime_service.py', '_build_runtime'),
    ('services/enterprise_assistant_runtime_service.py', '_result_for_runtime'),
    ('services/enterprise_assistant_runtime_service.py', '_finalize_runtime'),
    ('services/enterprise_assistant_runtime_service.py', '_live_role_snapshot'),
    ('services/enterprise_assistant_runtime_service.py', '_require_run_retry_controller'),
    ('services/enterprise_org_service.py', 'require_enterprise_member'),
    ('services/enterprise_org_service.py', 'require_group_manager'),
    ('services/enterprise_assistant_billing_group.py', 'capture'),
    ('services/billing_rate_catalog_service.py', 'rate_snapshot'),
    ('services/billing_metered_call.py', 'metered_call'),
    ('core/dependencies.py', 'require_super_admin'),
    ('core/dependencies.py', 'get_current_user'),
    ('core/security.py', 'decode_token'),
    ('core/super_admin.py', 'is_unique_super_admin'),
    ('services/rbac_service.py', 'is_super_admin_user'),
    ('api/v1/billing.py', 'propose_reservation_reconciliation'),
    ('api/v1/billing.py', 'decide_reservation_reconciliation'),
])
def test_reader_rejects_dependency_or_authorization_bypass(tmp_path, relative, function):
    app = ROOT / 'backend/app'
    for path in FINGERPRINTS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / path, target)
    target = tmp_path / relative
    tree = ast.parse(target.read_text(encoding='utf-8'))
    matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == function]
    assert len(matches) == 1
    matches[0].body = [ast.Return(value=ast.Constant(value=True))]
    target.write_text(ast.unparse(ast.fix_missing_locations(tree)), encoding='utf-8')
    assert not probe(tmp_path)


def test_reader_rejects_extra_monkeypatch_even_with_original_functions(tmp_path):
    app = ROOT / 'backend/app'
    for path in FINGERPRINTS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / path, target)
    target = tmp_path / 'services/billing_reservation_reconciliation_service.py'
    target.write_text(target.read_text(encoding='utf-8') + '\n_fenced_team_model_attempt = lambda *_: True\n',
                      encoding='utf-8')
    assert not probe(tmp_path)


def run_compatibility_gate(tmp_path, *, schema='absent', revision='120_discussion_turn_watermark',
                           capability='supported', marker=False, marker_symlink=False):
    import os
    import subprocess

    marker_path = tmp_path / 'activation.env'
    if marker:
        marker_path.write_text('activated\n', encoding='utf-8')
    if marker_symlink:
        marker_path.symlink_to(tmp_path / 'missing')
    common = ROOT / 'deploy/lib/common.sh'
    script = r'''
source "$1"
billing_evidence_database_state() { [[ "$SCHEMA" != broken ]] || return 1; printf '%s\n' "$SCHEMA"; }
current_alembic_revision() { printf '%s\n' "$REVISION"; }
stop_chat_authority_writers() { printf 'STOPPED_WRITERS\n' >&2; }
docker() { cat >/dev/null; [[ "$CAPABILITY" != error ]] || return 9; printf '%s\n' "$CAPABILITY"; }
assert_billing_evidence_database_compatible "$2"
if [[ "$BILLING_EVIDENCE_DATABASE_STATE" == complete ]]; then
  assert_billing_evidence_image_compatible "sha256:$(printf '%064d' 0)"
fi
printf 'ALLOWED:%s\n' "$BILLING_EVIDENCE_DATABASE_STATE"
'''
    return subprocess.run(['bash', '-c', script, '_', str(common), str(marker_path)],
                          env={**os.environ, 'SCHEMA': schema, 'REVISION': revision, 'CAPABILITY': capability},
                          text=True, capture_output=True, check=False)


def test_database_120_without_marker_allows_previous_reader(tmp_path):
    result = run_compatibility_gate(tmp_path, capability='unsupported')
    assert result.returncode == 0 and 'ALLOWED:absent' in result.stdout


def test_121_requires_reviewed_reader_and_accepts_it(tmp_path):
    good = run_compatibility_gate(tmp_path, schema='complete', revision='121_billing_model_evidence')
    assert good.returncode == 0 and 'ALLOWED:complete' in good.stdout
    for capability in ('unsupported', 'error', 'unknown'):
        result = run_compatibility_gate(tmp_path, schema='complete', revision='121_billing_model_evidence',
                                        capability=capability)
        assert result.returncode != 0 and 'ALLOWED:' not in result.stdout


def test_partial_migration_and_revision_mismatch_stop_writers(tmp_path):
    for schema, revision in [
        ('partial', '120_discussion_turn_watermark'),
        ('complete', '120_discussion_turn_watermark'),
        ('absent', '121_billing_model_evidence'),
        ('broken', '121_billing_model_evidence'),
        ('complete', 'unknown'),
    ]:
        result = run_compatibility_gate(tmp_path, schema=schema, revision=revision)
        assert result.returncode != 0 and 'STOPPED_WRITERS' in result.stderr
        assert 'ALLOWED:' not in result.stdout


def test_activation_marker_blocks_old_database_and_symlink(tmp_path):
    result = run_compatibility_gate(tmp_path, marker=True)
    assert result.returncode != 0 and 'STOPPED_WRITERS' in result.stderr
    (tmp_path / 'activation.env').unlink()
    result = run_compatibility_gate(tmp_path, marker_symlink=True)
    assert result.returncode != 0 and 'ALLOWED:' not in result.stdout


@pytest.mark.parametrize('method', ['_current', '_action_clarification', 'execute'])
def test_reader_rejects_assistant_tool_authorization_bypass(tmp_path, method):
    app = ROOT / 'backend/app'
    for path in FINGERPRINTS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / path, target)
    target = tmp_path / 'services/enterprise_assistant_runtime_service.py'
    tree = ast.parse(target.read_text(encoding='utf-8'))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'EnterpriseAssistantToolExecutor']
    assert len(classes) == 1
    matches = [node for node in classes[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == method]
    assert len(matches) == 1
    matches[0].body = [ast.Return(value=ast.Constant(value=True))]
    target.write_text(ast.unparse(ast.fix_missing_locations(tree)), encoding='utf-8')
    assert not probe(tmp_path)
