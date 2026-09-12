"""16项手动鉴权/归属补测与15项普通用户工坊拒绝；默认dry-run且不读取凭据。

只能使用既有QA107/110，正常登录前置6请求加白名单31请求，共37。
所有请求严格按顺序匹配；禁止默认Runner.run、任意path、令牌转发和失败重试。
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

CASES = [{'account': 'anonymous',
  'method': 'GET',
  'path': '/api/sandboxes/-1/preview/',
  'expected': 401,
  'payload': None,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': 'authenticate_preview_session decodes required cookie before environment/worker access'},
 {'account': 'anonymous',
  'method': 'HEAD',
  'path': '/api/sandboxes/-1/preview/',
  'expected': 401,
  'payload': None,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': 'authenticate_preview_session decodes required cookie before environment/worker access'},
 {'account': 'anonymous',
  'method': 'POST',
  'path': '/api/sandboxes/-1/preview/',
  'expected': 401,
  'payload': {},
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': 'authenticate_preview_session decodes required cookie before environment/worker access'},
 {'account': 'anonymous',
  'method': 'GET',
  'path': '/api/agents/events?replay=0',
  'expected': 401,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': '_resolve_sse_user precedes event_source subscription'},
 {'account': 'anonymous',
  'method': 'POST',
  'path': '/v1/responses',
  'expected': 401,
  'payload': {},
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': '_bearer_identity precedes upstream/store; POST must be valid JSON object'},
 {'account': 'anonymous',
  'method': 'GET',
  'path': '/v1/responses/-1',
  'expected': 401,
  'payload': None,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': '_bearer_identity precedes upstream/store; POST must be valid JSON object'},
 {'account': 'anonymous',
  'method': 'DELETE',
  'path': '/v1/responses/-1',
  'expected': 401,
  'payload': None,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': '_bearer_identity precedes upstream/store; POST must be valid JSON object'},
 {'account': 'anonymous',
  'method': 'GET',
  'path': '/v1/responses/-1/input_items',
  'expected': 401,
  'payload': None,
  'authorization': 'omit',
  'cookie': 'omit',
  'proof': '_bearer_identity precedes upstream/store; POST must be valid JSON object'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/reports/-1/export/word',
  'expected': 403,
  'proof': '_ensure_report_export_permission precedes report lookup; 404 is failure, no generation on deny'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/reports/-1/export/pdf',
  'expected': 403,
  'proof': '_ensure_report_export_permission precedes report lookup; 404 is failure, no generation on deny'},
 {'account': 'no_permission',
  'method': 'PUT',
  'path': '/api/forum/posts/-1/pin',
  'expected': 403,
  'payload': {'pinned': False},
  'proof': 'valid PinIn then admin role check before nonexistent -1 lookup; 404 means gate failed, not '
           'permission pass'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/rbac/users/107/roles',
  'expected': 403,
  'proof': 'cross-user check before data lookup; caller identity must be107-110 manifest110'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/rbac/users/107/permissions',
  'expected': 403,
  'proof': 'cross-user check before data lookup; caller identity must be107-110 manifest110'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/rbac/users/107/menus',
  'expected': 403,
  'proof': 'cross-user check before data lookup; caller identity must be107-110 manifest110'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/rbac/users/107/data-scope',
  'expected': 403,
  'proof': 'cross-user check before data lookup; caller identity must be107-110 manifest110'},
 {'account': 'no_permission',
  'method': 'GET',
  'path': '/api/users/107/avatar/image',
  'expected': 403,
  'proof': 'cross-user check before avatar existence'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/agents',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agents',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/agents/-1/versions',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/agent-versions/-1',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agents/-1/versions',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agent-versions/-1/skills',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'DELETE',
  'path': '/api/agent-studio/bindings/-1',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agent-versions/-1/test',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agent-versions/-1/submit',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/agent-versions/-1/withdraw',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/skills',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/skills',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/skills/-1/versions',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'GET',
  'path': '/api/agent-studio/skill-versions/-1',
  'expected': 403,
  'payload': None,
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'},
 {'account': 'owner_a',
  'method': 'POST',
  'path': '/api/agent-studio/skills/-1/versions',
  'expected': 403,
  'payload': {},
  'proof': 'require_studio_role router dependency precedes body validation and endpoint mutation; QA107 '
           'basicuser is not systemreviewer'}]
SOURCE_SHA256 = {'app/services/deepseek_responses_service.py': '0071d8e7fc33943f317bcbeb4b4067dfc49e3737a9faa387c16dc2ba3bc28cb6',
 'app/services/sandbox_service.py': 'ebc3dbc3dae7eb17890df91247eaf72e9ed26886eea72fcabfaf19939125fa4d',
 'app/api/v1/agents.py': '19562d2447e92dc9155c52c148dff4a3403060304c217c000b42baf4da1fe264',
 'app/core/dependencies.py': 'b75be1117e45d543d52fcae4bcadee16d1b93d214369beb7fd6e0a1c124f1709',
 'app/services/forum_service.py': '48c53c3153ed12255e3445ef84511fd4e44c7e963c6bb9eb15f29c45c8b21890',
 'app/api/v1/reports.py': '31c4e760f05875ee2e31fc8c0e29b564819e1679e60127dcbe47c5abb9757fce',
 'app/api/v1/rbac.py': 'd0f34037d29d7713172fed807705083f79ba4e815914482b015394ca9779ae3c',
 'app/api/v1/avatars.py': 'f59bf9dfb7e42d723aa052df2acee2a2c61ecc5ad7ed9403f737e876128fb159',
 'app/api/v1/agent_studio.py': '8ba24a9b1e4502c5e827c75cb325da2d5b26073f1e6e55728cf0fce9bab18e06',
 'app/services/agent_studio_service.py': '204a92ee28602b997aaa4dd2c2e67660315bc8e2a205dbfa031ae899913d78cb'}
ACCOUNT_IDS = {"owner_a": 107, "member_a": 108, "owner_b": 109, "no_permission": 110}
LOGIN_ACCOUNTS = ("owner_a", "no_permission")


def load_core(root):
    spec = importlib.util.spec_from_file_location(
        'blocked_contract_core', root / 'scripts/verify_permission_acceptance_https.py')
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core


def validate_cases():
    if len(CASES) != 31 or not SOURCE_SHA256:
        raise ValueError('frozen_case_cardinality')
    counts = collections.Counter((row['account'], row['expected']) for row in CASES)
    if counts != {('anonymous', 401): 8, ('no_permission', 403): 8, ('owner_a', 403): 15}:
        raise ValueError('frozen_account_contract')
    if len({(r['account'], r['method'], r['path']) for r in CASES}) != 31:
        raise ValueError('duplicate_case')
    for row in CASES:
        if row['path'].startswith('/v1/responses') and row['account'] != 'anonymous':
            raise ValueError('platform_token_must_not_enter_upstream_gateway')
        if not row['path'].startswith('/') or '//' in row['path'] or '{' in row['path']:
            raise ValueError('invalid_frozen_path')


def validate_manifest(manifest):
    if (manifest.get('marker') != '20260908' or manifest.get('role_code') != 'qa_permission_20260908'
            or set(manifest.get('accounts', {})) != set(ACCOUNT_IDS)):
        raise ValueError('wrong_qa_manifest')
    for account, expected_id in ACCOUNT_IDS.items():
        item = manifest['accounts'][account]
        if item.get('id') != expected_id or item.get('username') != f'qa_20260908_{account}':
            raise ValueError('wrong_qa_identity')


def sequence_for(manifest):
    sequence = []
    for account in LOGIN_ACCOUNTS:
        item = manifest['accounts'][account]
        sequence.append({'account': account, 'method': 'POST', 'path': '/api/auth/login', 'expected': 200,
                         'payload': {'username': item['username'], 'password': item['password']}})
        for suffix in ('roles', 'permissions'):
            sequence.append({'account': account, 'method': 'GET',
                             'path': f"/api/rbac/users/{item['id']}/{suffix}", 'expected': 200, 'payload': None})
    return sequence + CASES


def same_request(row, account, method, path, expected, payload, kwargs):
    # 显式token override可绕开anonymous，因此即使是有效凭据也禁止。
    if kwargs.keys() - {'purpose'}:
        return False
    return (row['account'] == account and row['method'] == method and row['path'] == path
            and row['expected'] == expected and row.get('payload') == payload)


def execute(core, base, manifest, plan, output):
    sequence = sequence_for(manifest)

    class StrictRunner(core.Runner):
        cursor = 0

        def request(self, account, method, path, expected=200, payload=None, **kwargs):
            if self.cursor >= len(sequence) or not same_request(
                    sequence[self.cursor], account, method, path, expected, payload, kwargs):
                raise ValueError('request_outside_exact_ordered_whitelist')
            if path.startswith('/v1/responses'):
                if account != 'anonymous' or self.tokens.get('anonymous'):
                    raise ValueError('upstream_platform_token_forbidden')
            self.cursor += 1
            return super().request(account, method, path, expected, payload, **kwargs)

    runner = StrictRunner(base, manifest, plan, output, phase='blocked_contracts_only')
    runner.log.update(planned_requests=37, planned_preflight_requests=6, planned_contract_requests=31,
                      reviewed_source_sha256=SOURCE_SHA256,
                      authentication_writes='QA107/110各一次登录更新认证状态/令牌版本/审计；不并发使用这两个QA会话',
                      safety_scope='仅精确白名单；资源写负向使用不存在的-1及源码证明的先验门禁；无正向业务写',
                      not_tested=['60项登录/资源能力功能正向', '公开功能', 'WebSocket真实会话',
                                  'SSE已鉴权生产事件隔离', '预览与Responses独立凭据正向', '系统审查者工坊正向'])
    try:
        runner.log['status'] = 'running'
        for account in LOGIN_ACCOUNTS:
            runner.login(account)
        for case in CASES:
            runner.request(case['account'], case['method'], case['path'], case['expected'],
                           case.get('payload'), purpose=case['proof'])
        runner.check(runner.cursor == 37 and len(runner.log['requests']) == 37, '精确完成6前置+31补测')
        runner.log['status'] = 'passed'
    except Exception as error:
        runner.log.update(status='failed', failure_type=type(error).__name__)
    finally:
        runner.log['finished_at'] = datetime.now(timezone.utc).isoformat()
        runner.log['summary'] = dict(collections.Counter(row['status'] for row in runner.log['requests']))
        runner.log['actual_requests'] = len(runner.log['requests'])
        runner.save()
        runner.tokens.clear()
    return {'status': runner.log['status'], 'actual_requests': runner.log['actual_requests'],
            'planned_requests': 37, 'summary': runner.log['summary']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--base-url', default='https://lijiadong.cn')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    validate_cases()
    root = args.source_root.resolve()
    core = load_core(root)
    for name, expected in SOURCE_SHA256.items():
        if core.sha((root / name).read_bytes()) != expected:
            raise ValueError('reviewed_source_changed:' + name)
    plan = core.private_json(args.plan)
    core.validate_plan(plan, root)
    if not args.execute:
        print(json.dumps({'status': 'plan_verified', 'requests_sent': 0, 'credentials_read': False,
                          'planned_requests': 37, 'preflight_requests': 6, 'contract_requests': 31}))
        return 0
    if not args.credentials or not args.output:
        parser.error('执行要求credentials和独占output路径')
    manifest = core.private_json(args.credentials)
    validate_manifest(manifest)
    result = execute(core, args.base_url, manifest, plan, args.output)
    print(json.dumps(result))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'failed', 'failure_type': type(error).__name__}))
        raise SystemExit(1) from None
