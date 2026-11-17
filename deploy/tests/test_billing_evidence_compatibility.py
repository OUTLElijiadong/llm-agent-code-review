"""Regression tests for 121 schema and reviewed-reader release gates."""
from __future__ import annotations

import ast
import copy
import json
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
import pytest
import billing_evidence_compatibility as compatibility
from billing_evidence_compatibility import EXPECTED_MODULES as FINGERPRINTS
from billing_evidence_compatibility import module_is_reviewed, probe
from billing_evidence_schema_compatibility import EXPECTED, classify

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


MYSQL_ESCAPED_CHECK = (
    r"(((`model_search_calls_evidence` is null) and (`model_fetch_calls_evidence` is null)) or "
    r"((`model_search_calls_evidence` is not null) and (`model_fetch_calls_evidence` is not null) and "
    r"(`model_search_calls_evidence` = 0) and (`model_fetch_calls_evidence` = 0) and "
    r"(`decision` in (_utf8mb4\'proposed\',_utf8mb4\'approved\',_utf8mb4\'rejected\')) "
    r"and (`resolution` = _utf8mb4\'settle\')))"
)


def test_mysql_8046_escaped_check_is_accepted():
    schema = complete_schema()
    schema['checks'][0]['clause'] = MYSQL_ESCAPED_CHECK
    assert classify(schema) == 'complete'


def test_mysql_8046_escaped_check_passes_json_cli():
    schema = complete_schema()
    schema['checks'][0]['clause'] = MYSQL_ESCAPED_CHECK
    result = subprocess.run(
        [sys.executable, str(ROOT / 'deploy/lib/billing_evidence_schema_compatibility.py')],
        input=json.dumps(schema), text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == 'complete'


@pytest.mark.parametrize('field,value', [
    ('nullable', 'NO'), ('type', 'int'), ('default', '0'), ('extra', 'DEFAULT_GENERATED'),
])
def test_escaped_check_does_not_bypass_column_checks(field, value):
    schema = complete_schema()
    schema['checks'][0]['clause'] = MYSQL_ESCAPED_CHECK
    schema['columns'][0][field] = value
    assert classify(schema) == 'partial'


@pytest.mark.parametrize('bad_clause', [
    MYSQL_ESCAPED_CHECK.replace(' = 0', ' = 1', 1),
    MYSQL_ESCAPED_CHECK + ' OR model_search_calls_evidence = 1',
    MYSQL_ESCAPED_CHECK.replace(' and (`resolution`', ' or (`resolution`'),
    MYSQL_ESCAPED_CHECK.replace(r"_utf8mb4\'approved\'", r"_utf8mb4\'unknown\'"),
    MYSQL_ESCAPED_CHECK.replace(r"_utf8mb4\'approved\'", r"_latin1\'approved\'"),
    MYSQL_ESCAPED_CHECK.replace(r"_utf8mb4\'approved\'", "_utf8mb4'approved'"),
    MYSQL_ESCAPED_CHECK.replace(r"\'approved\'", r"\\'approved\\'"),
    MYSQL_ESCAPED_CHECK.replace(r"\'approved\'", r"\'approved'"),
    MYSQL_ESCAPED_CHECK.replace(r"\'approved\'", r"\'approved\' OR 1 = 1"),
    MYSQL_ESCAPED_CHECK.replace(r"\'settle\'", r"\'settle\' COLLATE utf8mb4_bin"),
    MYSQL_ESCAPED_CHECK.replace(' is null', r' is \null', 1),
])
def test_mysql_escaped_check_rejects_unexpected_changes(bad_clause):
    schema = complete_schema()
    schema['checks'][0]['clause'] = bad_clause
    assert classify(schema) == 'partial'


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


def test_platform_cost_gate_accepts_baseline_and_reviewed_hotfix_only():
    path = 'services/billing_runtime_gate.py'
    assert module_is_reviewed(path, FINGERPRINTS[path])
    assert module_is_reviewed(path, 'dca4a319ec20b8628bb20d088bcb167ce14d99e94ddabb416afbed1906f9d16a')
    assert module_is_reviewed(path, 'a137a20730865a1805351d9c5aa0bc3889c4bbcd73e55d61481f971423615581')
    assert not module_is_reviewed(path, '0' * 64)


FINGERPRINT_PATTERN = re.compile(r'[0-9a-f]{64}\Z')


def fingerprint_problem(value) -> str | None:
    """Explain why *value* is not an acceptable reviewed pin, or None when it is one.

    Pins are compared by digest equality, so a 66-character value matches nothing and
    silently turns the whole gate into `unsupported` for every shipped image.
    """
    if not isinstance(value, str):
        return f'expected str, got {type(value).__name__}'
    if len(value) != 64:
        return f'expected 64 hex characters, got {len(value)}'
    if not FINGERPRINT_PATTERN.fullmatch(value):
        return 'expected lowercase hexadecimal characters (0-9a-f) only'
    return None


def pinned_fingerprints(gate=compatibility):
    """Yield ``(origin, digest)`` for every non-empty pin in *gate*, in a stable order.

    ``REVIEWED_ENTRY_PROFILES`` leaves the uvicorn log-filter slot empty when the hook
    is absent, so falsy slots are skipped rather than pinned.
    """
    for path in sorted(gate.EXPECTED_MODULES):
        if gate.EXPECTED_MODULES[path]:
            yield f'EXPECTED_MODULES[{path!r}]', gate.EXPECTED_MODULES[path]
    for path in sorted(gate.REVIEWED_MODULE_PROFILES):
        for digest in sorted(filter(None, gate.REVIEWED_MODULE_PROFILES[path])):
            yield f'REVIEWED_MODULE_PROFILES[{path!r}]', digest
    profiles = sorted(gate.REVIEWED_ENTRY_PROFILES, key=lambda slots: tuple(slot or '' for slot in slots))
    for index, profile in enumerate(profiles):
        for slot, digest in enumerate(profile):
            if digest:
                yield f'REVIEWED_ENTRY_PROFILES[{index}][{slot}]', digest


PINNED_FINGERPRINTS = list(pinned_fingerprints())


@pytest.mark.parametrize(
    'origin,digest', PINNED_FINGERPRINTS, ids=[origin for origin, _ in PINNED_FINGERPRINTS],
)
def test_pinned_fingerprint_is_exactly_64_lowercase_hex(origin, digest):
    """Every pin in every compatibility container is a bare sha256 hex digest."""
    problem = fingerprint_problem(digest)
    assert problem is None, f'{origin} pins an invalid fingerprint: {problem}: {digest!r}'


def test_fingerprint_inventory_covers_every_compatibility_container():
    """A scan that silently finds nothing would let the next mistyped pin through."""
    expected = (
        len(compatibility.EXPECTED_MODULES)
        + sum(len(digests) for digests in compatibility.REVIEWED_MODULE_PROFILES.values())
        + sum(1 for profile in compatibility.REVIEWED_ENTRY_PROFILES for slot in profile if slot)
    )
    assert compatibility.EXPECTED_MODULES and compatibility.REVIEWED_MODULE_PROFILES
    assert compatibility.REVIEWED_ENTRY_PROFILES
    assert expected == len(PINNED_FINGERPRINTS) > 0


def test_scan_flags_66_character_pins_in_every_compatibility_container(monkeypatch):
    """Reproduce the root cause: a reviewed pin stored two characters too long."""
    gateway = 'services/billing_runtime_gate.py'
    entry = sorted(compatibility.REVIEWED_ENTRY_PROFILES,
                   key=lambda slots: tuple(slot or '' for slot in slots))[0]
    oversize_entry = tuple((slot or '') + 'ab' if slot else slot for slot in entry)
    monkeypatch.setitem(compatibility.EXPECTED_MODULES, 'services/rbac_service.py',
                        compatibility.EXPECTED_MODULES['services/rbac_service.py'] + 'ab')
    monkeypatch.setitem(compatibility.REVIEWED_MODULE_PROFILES, gateway,
                        {sorted(compatibility.REVIEWED_MODULE_PROFILES[gateway])[0] + 'ab'})
    monkeypatch.setattr(compatibility, 'REVIEWED_ENTRY_PROFILES', {oversize_entry})

    flagged = [(origin, fingerprint_problem(digest))
               for origin, digest in pinned_fingerprints() if fingerprint_problem(digest)]
    assert [origin.split('[')[0] for origin, _ in flagged] == [
        'EXPECTED_MODULES', 'REVIEWED_MODULE_PROFILES',
        *['REVIEWED_ENTRY_PROFILES'] * sum(1 for slot in entry if slot),
    ]
    assert {problem for _, problem in flagged} == {'expected 64 hex characters, got 66'}


@pytest.mark.parametrize('change', ['old_entry', 'remove_api_router', 'override_entry'])
def test_reader_rejects_changed_application_entry(tmp_path, change):
    """The reviewed startup fingerprint stays strict after adding log filtering."""
    app = ROOT / 'backend/app'
    for relative in FINGERPRINTS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / relative, target)
    assert probe(tmp_path)
    target = tmp_path / 'main.py'
    tree = ast.parse(target.read_text(encoding='utf-8'))
    if change == 'old_entry':
        original_count = len(tree.body)
        tree.body = [node for node in tree.body if not (
            isinstance(node, ast.ImportFrom) and node.module == 'app.core.uvicorn_log_filter'
            or isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == 'install_uvicorn_asgi_error_filter'
        )]
        assert len(tree.body) == original_count - 2
    elif change == 'remove_api_router':
        matches = [node for node in tree.body if isinstance(node, ast.Expr)
                   and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                   and node.value.func.attr == 'include_router'
                   and node.value.args and isinstance(node.value.args[0], ast.Name)
                   and node.value.args[0].id == 'api_router']
        assert len(matches) == 1
        tree.body.remove(matches[0])
    else:
        tree.body.extend(ast.parse('app.include_router = lambda *args, **kwargs: None').body)
    target.write_text(ast.unparse(ast.fix_missing_locations(tree)), encoding='utf-8')
    assert not probe(tmp_path)


def _reader_fixture_with_log_dependencies(tmp_path):
    app = ROOT / 'backend/app'
    for relative in set(FINGERPRINTS) | {
        'core/error_handlers.py', 'core/uvicorn_log_filter.py', 'services/billing_statement_gateway.py',
    }:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / relative, target)


def _legacy_4153_entry_and_handlers(root):
    """Build the reviewed old AST without requiring Git history in CI."""
    main = ast.parse((root / 'main.py').read_text(encoding='utf-8'))
    main.body = [node for node in main.body if not (
        isinstance(node, ast.ImportFrom) and node.module == 'app.core.uvicorn_log_filter'
        or isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'install_uvicorn_asgi_error_filter'
    )]
    handlers = ast.parse((root / 'core/error_handlers.py').read_text(encoding='utf-8'))
    handlers.body = [node for node in handlers.body if not (
        isinstance(node, ast.Import) and [alias.name for alias in node.names] == ['re']
        or isinstance(node, ast.FunctionDef) and node.name == '_safe_error_locations'
    )]
    register = next(node for node in handlers.body if isinstance(node, ast.FunctionDef) and node.name == 'register_handlers')
    unhandled = next(node for node in register.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == 'unhandled_handler')
    content_index = next(index for index, node in enumerate(unhandled.body)
                         if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                         and node.target.id == 'content')
    unhandled.body = [unhandled.body[0], *ast.parse('logger.exception(exc)\nrequest_id = get_request_id(req)').body,
                      *unhandled.body[content_index:]]
    return ast.unparse(ast.fix_missing_locations(main)), ast.unparse(ast.fix_missing_locations(handlers))


def _legacy_billing_api(root):
    """Reconstruct the reviewed 4.1.54/4.1.55 API from the candidate AST."""
    tree = ast.parse((root / 'api/v1/billing.py').read_text(encoding='utf-8'))
    func = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'download_payment_statement')
    outer_try = next(node for node in func.body if isinstance(node, ast.Try))
    channel_branch = next(node for node in outer_try.body if isinstance(node, ast.If))
    removed = [node for node in channel_branch.body if isinstance(node, ast.If)
               and any(isinstance(child, ast.Attribute) and child.attr == 'alipay_gateway_mode'
                       for child in ast.walk(node.test))]
    assert len(removed) == 1
    channel_branch.body.remove(removed[0])
    calls = [node for node in ast.walk(channel_branch) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'download_alipay_trade_statement']
    assert len(calls) == 1
    calls[0].keywords = [item for item in calls[0].keywords if item.arg != 'sandbox']
    return ast.unparse(ast.fix_missing_locations(tree))


def _legacy_statement_gateway(root):
    """Reconstruct the gateway used by 4.1.53 through 4.1.55."""
    tree = ast.parse((root / 'services/billing_statement_gateway.py').read_text(encoding='utf-8'))
    func = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'download_alipay_trade_statement')
    index = next(i for i, arg in enumerate(func.args.kwonlyargs) if arg.arg == 'sandbox')
    func.args.kwonlyargs.pop(index)
    func.args.kw_defaults.pop(index)
    guard = next(node for node in func.body if isinstance(node, ast.If)
                 and any(isinstance(child, ast.Name) and child.id == 'sandbox' for child in ast.walk(node.test)))
    func.body.remove(guard)
    return ast.unparse(ast.fix_missing_locations(tree))


def test_reader_rejects_billing_api_and_statement_gateway_mixed_versions(tmp_path):
    import hashlib
    from itertools import product

    _reader_fixture_with_log_dependencies(tmp_path)
    billing = tmp_path / 'api/v1/billing.py'
    gateway = tmp_path / 'services/billing_statement_gateway.py'
    new_billing, old_billing = billing.read_text(encoding='utf-8'), _legacy_billing_api(tmp_path)
    new_gateway, old_gateway = gateway.read_text(encoding='utf-8'), _legacy_statement_gateway(tmp_path)
    assert hashlib.sha256(ast.dump(ast.parse(old_gateway), include_attributes=False).encode()).hexdigest() == (
        '332eabeffd61f97eee705a413e19a6decf00caaf04ab536e7a8e136cbeda4d84'
    )
    for api, service in product(('old', 'new'), ('old', 'new', 'altered')):
        billing.write_text(old_billing if api == 'old' else new_billing, encoding='utf-8')
        gateway.write_text(old_gateway if service == 'old' else new_gateway + (
            '\nprobe_bypass = True\n' if service == 'altered' else ''), encoding='utf-8')
        assert probe(tmp_path) == ((api, service) in {('old', 'old'), ('new', 'new')})


@pytest.mark.parametrize('relative', ['core/uvicorn_log_filter.py', 'core/error_handlers.py'])
@pytest.mark.parametrize('change', ['missing', 'altered'])
def test_reader_rejects_missing_or_altered_logging_dependency(tmp_path, relative, change):
    _reader_fixture_with_log_dependencies(tmp_path)
    assert probe(tmp_path)
    target = tmp_path / relative
    if change == 'missing':
        target.unlink()
    else:
        target.write_text(target.read_text(encoding='utf-8') + '\nprobe_bypass = True\n', encoding='utf-8')
    try:
        assert not probe(tmp_path)
    except FileNotFoundError:
        pass


def test_reader_accepts_only_reviewed_entry_dependency_combinations(tmp_path):
    import hashlib
    from itertools import product

    _reader_fixture_with_log_dependencies(tmp_path)
    main_path, handler_path, filter_path = (tmp_path / 'main.py', tmp_path / 'core/error_handlers.py',
                                             tmp_path / 'core/uvicorn_log_filter.py')
    new_main, new_handler, new_filter = (path.read_text(encoding='utf-8') for path in (main_path, handler_path, filter_path))
    old_main, old_handler = _legacy_4153_entry_and_handlers(tmp_path)
    billing_path = tmp_path / 'api/v1/billing.py'
    new_billing, old_billing = billing_path.read_text(encoding='utf-8'), _legacy_billing_api(tmp_path)
    gateway_path = tmp_path / 'services/billing_statement_gateway.py'
    new_gateway, old_gateway = gateway_path.read_text(encoding='utf-8'), _legacy_statement_gateway(tmp_path)
    def digest(source):
        return hashlib.sha256(ast.dump(ast.parse(source), include_attributes=False).encode()).hexdigest()
    # Verified independently against release commit 38f181c86c3f74f40ce73c43ce73ff23b0f6a292.
    assert digest(old_main) == '0debff8636df3f2fb8759dd1ebd8cf6145e3abfb4cddd252782a383715e02c5d'
    assert digest(old_handler) == '33e6b79d3e605b840564af8e26da07b16e2246506df5cd65476ad96d0b3c711c'
    # Verified against both release 4.1.54 and the 4.1.55 base commit.
    assert digest(old_billing) == '2348f600c2dc7043d22b1b47be0d1fcbb17d12f28fd9c958e9ada9ad23c908eb'
    for entry, handler, hook, billing_api in product(
        ('old', 'new'), ('old', 'new'), ('absent', 'new', 'altered'), ('old', 'new', 'altered'),
    ):
        main_path.write_text(old_main if entry == 'old' else new_main, encoding='utf-8')
        handler_path.write_text(old_handler if handler == 'old' else new_handler, encoding='utf-8')
        billing_path.write_text(old_billing if billing_api == 'old' else new_billing + (
            '\nprobe_bypass = True\n' if billing_api == 'altered' else ''), encoding='utf-8')
        gateway_path.write_text(old_gateway if billing_api == 'old' else new_gateway, encoding='utf-8')
        if hook == 'absent':
            filter_path.unlink(missing_ok=True)
        else:
            filter_path.write_text(new_filter + ('\nprobe_bypass = True\n' if hook == 'altered' else ''), encoding='utf-8')
        try:
            supported = probe(tmp_path)
        except FileNotFoundError:
            supported = False
        assert supported == ((entry, handler, hook, billing_api) in {
            ('old', 'old', 'absent', 'old'),
            ('new', 'new', 'new', 'old'),
            ('new', 'new', 'new', 'new'),
        })


def test_image_gate_runs_real_probe_for_old_new_and_mixed_readers(tmp_path):
    import os

    _reader_fixture_with_log_dependencies(tmp_path)
    main_path = tmp_path / 'main.py'
    handler_path = tmp_path / 'core/error_handlers.py'
    hook_path = tmp_path / 'core/uvicorn_log_filter.py'
    new_main = main_path.read_text(encoding='utf-8')
    new_handler = handler_path.read_text(encoding='utf-8')
    new_hook = hook_path.read_text(encoding='utf-8')
    old_main, old_handler = _legacy_4153_entry_and_handlers(tmp_path)
    billing_path = tmp_path / 'api/v1/billing.py'
    new_billing = billing_path.read_text(encoding='utf-8')
    old_billing = _legacy_billing_api(tmp_path)
    gateway_path = tmp_path / 'services/billing_statement_gateway.py'
    new_gateway = gateway_path.read_text(encoding='utf-8')
    old_gateway = _legacy_statement_gateway(tmp_path)
    script = r'''
source "$1"
docker() { "$TEST_PYTHON" -; }
assert_billing_evidence_image_compatible "sha256:$(printf '%064d' 0)"
'''
    for entry, handler, hook, billing_api, gateway, expected in [
        (new_main, new_handler, new_hook, new_billing, new_gateway, True),
        (new_main, new_handler, new_hook, old_billing, old_gateway, True),
        (old_main, old_handler, None, old_billing, old_gateway, True),
        (new_main, new_handler, new_hook, old_billing, new_gateway, False),
        (new_main, new_handler, new_hook, new_billing, old_gateway, False),
        (old_main, old_handler, None, new_billing, new_gateway, False),
        (new_main, new_handler, new_hook, new_billing + '\nprobe_bypass = True\n', new_gateway, False),
        (old_main, new_handler, new_hook, old_billing, old_gateway, False),
        (new_main, old_handler, new_hook, old_billing, old_gateway, False),
        (new_main, new_handler, None, old_billing, old_gateway, False),
    ]:
        main_path.write_text(entry, encoding='utf-8')
        handler_path.write_text(handler, encoding='utf-8')
        billing_path.write_text(billing_api, encoding='utf-8')
        gateway_path.write_text(gateway, encoding='utf-8')
        if hook is None:
            hook_path.unlink(missing_ok=True)
        else:
            hook_path.write_text(hook, encoding='utf-8')
        result = subprocess.run(['bash', '-c', script, '_', str(ROOT / 'deploy/lib/common.sh')],
                                env={**os.environ, 'TEST_PYTHON': sys.executable, 'PRISM_APP_ROOT': str(tmp_path)},
                                capture_output=True, text=True, check=False)
        assert (result.returncode == 0) is expected


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


def test_122_known_descendant_keeps_complete_121_schema_with_activation_marker(tmp_path):
    result = run_compatibility_gate(
        tmp_path,
        schema='complete',
        revision='122_platform_model_cost_ledger',
        marker=True,
    )
    assert result.returncode == 0 and 'ALLOWED:complete' in result.stdout


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
