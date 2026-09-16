"""使用真实Runner与内存HTTP客户端验证白名单、脱敏、停止行为；不联网。"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

FOLDER = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('blocked_executor', FOLDER / 'execute_blocked_contracts_https.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CORE = MODULE.load_core(FOLDER.parents[2] / 'backend')


def manifest():
    return {'marker': '20260908', 'role_code': 'qa_permission_20260908',
            'accounts': {account: {'id': user_id, 'username': f'qa_20260908_{account}',
                                   'password': 'offline-secret-password'}
                         for account, user_id in MODULE.ACCOUNT_IDS.items()}}


class Response(io.BytesIO):
    def __init__(self, value, status):
        super().__init__(json.dumps(value).encode())
        self.code = status
        self.headers = {'X-Request-Id': 'offline-response-id'}


class Client:
    def __init__(self, credentials, fail_at=None):
        self.credentials = credentials
        self.sequence = MODULE.sequence_for(credentials)
        self.calls = []
        self.fail_at = fail_at

    def open(self, request, timeout):
        row = self.sequence[len(self.calls)]
        self.calls.append((request.method, request.full_url))
        if row['account'] == 'anonymous':
            assert request.get_header('Authorization') is None
            assert request.get_header('Cookie') is None
        if self.fail_at == len(self.calls):
            return Response({'code': 40400, 'message': 'offline-secret-body'}, 404)
        if row['path'] == '/api/auth/login':
            item = self.credentials['accounts'][row['account']]
            assert json.loads(request.data)['password'] == item['password']
            data = {'access_token': 'offline-secret-token', 'user': {
                'id': item['id'], 'username': item['username'], 'role': 'user', 'status': 1}}
        elif row['path'].endswith('/roles') and row['expected'] == 200:
            data = [] if row['account'] == 'no_permission' else [{'code': 'qa_permission_20260908'}]
        elif row['path'].endswith('/permissions') and row['expected'] == 200:
            data = [] if row['account'] == 'no_permission' else sorted(CORE.EXPECTED_PERMISSIONS)
        else:
            data = None
        return Response({'code': 0, 'data': data}, row['expected'])


class ExecutorContracts(unittest.TestCase):
    def run_executor(self, fail_at=None):
        credentials = manifest()
        client = Client(credentials, fail_at)
        with tempfile.TemporaryDirectory(prefix='blocked-contracts-offline-') as folder:
            output = Path(folder) / 'result.json'
            with patch.object(CORE.urllib.request, 'build_opener', return_value=client), patch.object(CORE.time, 'sleep'):
                result = MODULE.execute(CORE, 'https://offline.invalid', credentials, {'schema': 2}, output)
            raw = output.read_text()
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('offline-secret-password', raw)
            self.assertNotIn('offline-secret-token', raw)
            self.assertNotIn('offline-secret-body', raw)
            return result, json.loads(raw), client.calls

    def test_complete_exactly_37_and_only_two_logins(self):
        MODULE.validate_cases()
        MODULE.validate_manifest(manifest())
        result, log, calls = self.run_executor()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(calls), 37)
        self.assertEqual(sum(path.endswith('/api/auth/login') for _, path in calls), 2)
        self.assertEqual(sum(r['expected'] == 401 for r in log['requests']), 8)
        self.assertEqual(sum(r['expected'] == 403 for r in log['requests']), 23)

    def test_unexpected_404_stops_at_first_failure_without_retry(self):
        result, log, calls = self.run_executor(fail_at=7)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(calls), 7)
        self.assertEqual(log['summary'], {'passed': 6, 'failed': 1})

    def test_non_whitelisted_path_method_body_or_token_override_rejected(self):
        row = MODULE.CASES[0]
        args = (row['account'], row['method'], row['path'], row['expected'], row.get('payload'))
        self.assertTrue(MODULE.same_request(row, *args, {}))
        for field, value in ((0, 'owner_a'), (1, 'PATCH'), (2, '/api/projects/164'), (3, 200), (4, {'x': 1})):
            changed = list(args)
            changed[field] = value
            self.assertFalse(MODULE.same_request(row, *changed, {}))
        self.assertFalse(MODULE.same_request(row, *args, {'token': 'platform-jwt'}))

    def test_modified_case_or_credentials_fail_closed(self):
        with patch.object(MODULE, 'CASES', MODULE.CASES[:-1]), self.assertRaises(ValueError):
            MODULE.validate_cases()
        broken = copy.deepcopy(manifest())
        broken['accounts']['owner_a']['id'] = 1
        with self.assertRaises(ValueError):
            MODULE.validate_manifest(broken)


if __name__ == '__main__':
    unittest.main(verbosity=2)
