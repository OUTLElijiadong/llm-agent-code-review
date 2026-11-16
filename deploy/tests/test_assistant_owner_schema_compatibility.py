"""122 ownership guard tests; fixture is real MySQL 8.0 metadata, no data rows."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('assistant_owner_schema', ROOT / 'deploy/lib/assistant_owner_schema_compatibility.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
FIXTURE = json.loads((ROOT / 'deploy/tests/fixtures/assistant_owner_mysql8_schema.json').read_text())


def test_mysql_metadata_is_complete_and_empty_is_absent():
    assert probe.classify(FIXTURE) == 'complete'
    assert probe.classify({key: [] for key in FIXTURE}) == 'absent'


@pytest.mark.parametrize('collection', ['tables', 'columns', 'foreign_keys', 'checks', 'triggers'])
def test_missing_object_is_partial(collection):
    snapshot = copy.deepcopy(FIXTURE)
    snapshot[collection].pop()
    assert probe.classify(snapshot) == 'partial'


@pytest.mark.parametrize('mutation', [
    lambda s: s['tables'][0].update(type='VIEW'),
    lambda s: s['columns'][0].update(nullable='BAD'),
    lambda s: s['columns'][0].update(default='0'),
    lambda s: next(r for r in s['indexes'] if r['non_unique'] == 0).update(sub_part=8),
    lambda s: s['foreign_keys'][0].update(same_schema=False),
    lambda s: s['foreign_keys'][0].update(delete_rule='CASCADE'),
    lambda s: s['checks'][0].update(enforced='NO'),
    lambda s: s['checks'][0].update(clause='1 = 1'),
    lambda s: s['triggers'][0].update(statement="SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'different'"),
    lambda s: s['triggers'].append(dict(s['triggers'][0])),
])
def test_weakened_or_ambiguous_schema_fails_closed(mutation):
    snapshot = copy.deepcopy(FIXTURE)
    mutation(snapshot)
    assert probe.classify(snapshot) == 'partial'


def test_mysql_escaped_literals_and_unknown_sql():
    original = "((usage_kind = _utf8mb4'member') AND (payer_user_id IS NOT NULL) AND (payer_user_id = initiator_user_id)) OR ((usage_kind = _utf8mb4'enterprise_auto') AND (payer_user_id IS NULL))"
    escaped = original.replace("_utf8mb4'", "_utf8mb4\\'").replace("member'", "member\\'").replace("enterprise_auto'", "enterprise_auto\\'")
    assert probe.parse_check(original) == probe.parse_check(escaped)
    with pytest.raises(ValueError):
        probe.parse_check(escaped + " OR (1 = 1) --")


@pytest.mark.parametrize(('schema', 'revision', 'marker', 'ok', 'expected'), [
    ('absent', '121_billing_model_evidence', None, True, 'absent'),
    ('absent', '121_billing_model_evidence', 'pending', True, 'pending'),
    ('absent', '121_billing_model_evidence', 'active', False, ''),
    ('partial', '121_billing_model_evidence', 'pending', False, ''),
    ('complete', '121_billing_model_evidence', 'pending', False, ''),
    ('complete', '122_assistant_call_owner', 'pending', True, 'complete'),
    ('complete', '122_assistant_call_owner', 'active', True, 'complete'),
    ('partial', '122_assistant_call_owner', 'active', False, ''),
    ('absent', '122_assistant_call_owner', None, False, ''),
    ('complete', 'ambiguous', 'active', False, ''),
    ('complete', '122_assistant_call_owner', 'corrupt', False, ''),
])
def test_revision_marker_and_failure_injection(tmp_path, schema, revision, marker, ok, expected):
    path = tmp_path / 'marker'
    stopped = tmp_path / 'stopped'
    if marker:
        path.write_text(marker + '\n')
    command = '''
source deploy/lib/common.sh
assistant_owner_database_state() { printf '%s\\n' "$SCHEMA"; }
current_alembic_revision() { printf '%s\\n' "$REVISION"; }
assistant_owner_has_rows() { echo 0; }
stop_chat_authority_writers() { touch "$STOPPED"; }
assert_assistant_owner_database_compatible "$MARKER"
printf 'STATE=%s\\n' "$ASSISTANT_OWNER_DATABASE_STATE"
'''
    import os
    env = {**os.environ, 'SCHEMA': schema, 'REVISION': revision, 'MARKER': str(path), 'STOPPED': str(stopped)}
    result = subprocess.run(['bash', '-c', command], cwd=ROOT, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == ok, result.stderr
    if ok:
        assert f'STATE={expected}' in result.stdout
    else:
        assert stopped.exists()


def test_unproven_image_reader_is_rejected():
    result = subprocess.run(['bash', '-c', 'source deploy/lib/common.sh; assert_assistant_owner_image_compatible sha256:old'], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0


def test_marker_symlink_fails_closed(tmp_path):
    target = tmp_path / 'target'
    target.write_text('pending\n')
    marker = tmp_path / 'marker'
    marker.symlink_to(target)
    stopped = tmp_path / 'stopped'
    import os
    env = {**os.environ, 'MARKER': str(marker), 'STOPPED': str(stopped)}
    cmd = '''source deploy/lib/common.sh
assistant_owner_database_state() { echo absent; }
current_alembic_revision() { echo 121_billing_model_evidence; }
stop_chat_authority_writers() { touch "$STOPPED"; }
assert_assistant_owner_database_compatible "$MARKER"
'''
    result = subprocess.run(['bash', '-c', cmd], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert stopped.exists()


def test_partial_unique_index_positions_fail_closed():
    snapshot = copy.deepcopy(FIXTURE)
    row = next(r for r in snapshot['indexes'] if r['name'] == 'uq_ent_run_owner_generation' and r['seq'] == 2)
    row['seq'] = 3
    assert probe.classify(snapshot) == 'partial'


def test_identifier_quotes_cannot_normalize_changed_string_literal():
    correct = "usage_kind = _utf8mb4'member'"
    changed = "usage_kind = _utf8mb4'mem`x`ber'"
    assert probe.parse_check(correct) != probe.parse_check(changed)


def test_literal_case_is_not_silently_folded():
    assert probe.parse_check("usage_kind = 'member'") != probe.parse_check("usage_kind = 'MEMBER'")
    assert probe.parse_check("`USAGE_KIND` = 'member'") == probe.parse_check("usage_kind = 'member'")


@pytest.mark.parametrize('result', ['1', 'bad', ''])
def test_disabled_foundation_rejects_existing_or_unreadable_ownership(tmp_path, result):
    import os
    env = {**os.environ, 'MARKER': str(tmp_path / 'marker'), 'STOPPED': str(tmp_path / 'stopped'), 'ROWS': result}
    cmd = '''source deploy/lib/common.sh
assistant_owner_database_state() { echo complete; }
current_alembic_revision() { echo 122_assistant_call_owner; }
assistant_owner_has_rows() { printf '%s\\n' "$ROWS"; }
stop_chat_authority_writers() { touch "$STOPPED"; }
assert_assistant_owner_database_compatible "$MARKER"
'''
    output = subprocess.run(['bash', '-c', cmd], cwd=ROOT, env=env, capture_output=True, text=True)
    assert output.returncode != 0
    assert (tmp_path / 'stopped').exists()
