"""仅用本轮专用账号核验任务读取404/401/403与恢复信息，无模型或任务写入。"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/app/scripts')
from verify_permission_acceptance_https import ACCOUNTS, Runner, private_json  # noqa: E402

MARKER = '20260908'
ERROR_FIELDS = {'code', 'message', 'request_id', 'retryable', 'next_action'}
NOT_FOUND_MESSAGE = '审查任务不存在或当前账号无权访问'
NOT_FOUND_ACTION = '请返回审查记录列表重新选择；如需访问，请联系项目负责人确认权限'
REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')


def validate_manifest(manifest):
    """登录前绑定本轮专用身份；Runner.login随后核对服务端实际ID、状态和角色。"""
    if not isinstance(manifest, dict) or manifest.get('marker') != MARKER:
        raise ValueError('qa_manifest_marker_mismatch')
    if manifest.get('role_code') != f'qa_permission_{MARKER}':
        raise ValueError('qa_manifest_role_mismatch')
    accounts = manifest.get('accounts')
    if not isinstance(accounts, dict) or set(accounts) != set(ACCOUNTS):
        raise ValueError('qa_manifest_accounts_mismatch')
    ids = set()
    for account in ACCOUNTS:
        item = accounts[account]
        if not isinstance(item, dict):
            raise ValueError('qa_manifest_account_invalid')
        user_id = item.get('id')
        if type(user_id) is not int or user_id <= 0 or user_id in ids:
            raise ValueError('qa_manifest_id_invalid')
        if item.get('username') != f'qa_{MARKER}_{account}':
            raise ValueError('qa_manifest_username_mismatch')
        if not isinstance(item.get('password'), str) or not item['password']:
            raise ValueError('qa_manifest_password_missing')
        ids.add(user_id)


def validate_error(data, expected, server_rid, suffix):
    """按真实AppError信封校验，额外字段或内部detail不能被一致性比较掩盖。"""
    if not isinstance(data, dict) or not ERROR_FIELDS.issubset(data):
        raise ValueError('error_envelope_missing_fields')
    if set(data) - ERROR_FIELDS - {'detail'} or data.get('detail') is not None:
        raise ValueError('error_envelope_private_or_unexpected_fields')
    if data['request_id'] != server_rid or data['retryable'] is not False:
        raise ValueError('error_envelope_context_mismatch')
    expected_code = {401: 40100, 403: 40303, 404: 40400}[expected]
    if type(data['code']) is not int or data['code'] != expected_code:
        raise ValueError('business_error_contract')
    if expected == 401:
        message, action = '缺少token', '请重新登录后再试'
    elif expected == 403:
        permission = 'issue:view' if suffix else 'review:view'
        message, action = f'无操作权限: 需要 {permission}', '请联系管理员确认权限'
    else:
        message, action = NOT_FOUND_MESSAGE, NOT_FOUND_ACTION
    if data['message'] != message or data['next_action'] != action:
        raise ValueError('error_recovery_contract')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = private_json(args.credentials)
    validate_manifest(manifest)
    runner = Runner('https://lijiadong.cn', manifest, {}, args.output, phase='task-access')
    try:
        runner.log['status'] = 'running'
        for account in ACCOUNTS:
            runner.login(account)
        errors404 = []
        for account in ['anonymous', 'no_permission', 'owner_a', 'member_a', 'owner_b']:
            expected = 401 if account == 'anonymous' else 403 if account == 'no_permission' else 404
            for task_id in [-1, 161, 162]:
                for suffix in ['', '/issues']:
                    path = f'/api/review/tasks/{task_id}{suffix}'
                    rid = 'qa-' + uuid.uuid4().hex
                    headers = {'Accept': 'application/json', 'X-Request-Id': rid}
                    if account != 'anonymous':
                        headers['Authorization'] = 'Bearer ' + runner.tokens[account]
                    row = {'account': account, 'method': 'GET', 'path': path, 'expected': expected,
                           'status': 'pending', 'request_id': rid}
                    runner.log['requests'].append(row)
                    runner.save()
                    try:
                        try:
                            response = runner.client.open(
                                urllib.request.Request(runner.base + path, headers=headers), timeout=20,
                            )
                        except urllib.error.HTTPError as exc:
                            response = exc
                        with response:
                            raw = response.read(8193)
                            row.update(actual=response.code, response_bytes=len(raw),
                                       response_sha256=hashlib.sha256(raw).hexdigest(),
                                       response_request_id=response.headers.get('X-Request-Id'))
                            if response.code != expected or len(raw) > 8192:
                                raise ValueError('unexpected_task_error_response')
                            # Nginx 用可信 $request_id 覆盖客户端编号；验收响应头与正文的同一编号。
                            server_rid = row['response_request_id']
                            if not isinstance(server_rid, str) or not REQUEST_ID_PATTERN.fullmatch(server_rid):
                                raise ValueError('response_request_id_invalid')
                            data = json.loads(raw)
                            validate_error(data, expected, server_rid, suffix)
                            if expected == 404:
                                errors404.append({key: value for key, value in data.items() if key != 'request_id'})
                        row['status'] = 'passed'
                    except Exception as exc:
                        row['status'] = 'failed'
                        row['failure_type'] = type(exc).__name__
                        raise
                    finally:
                        runner.save()
                    time.sleep(0.15)
        if len(errors404) != 18 or not all(value == errors404[0] for value in errors404):
            raise ValueError('not_found_envelopes_differ')
        runner.check(True, '缺失与不可见任务两个GET同一404信封，未返回项目存在信息')
        runner.log['status'] = 'passed'
    except Exception as exc:
        runner.log['status'] = 'failed'
        runner.log['failure_type'] = type(exc).__name__
        raise RuntimeError('task_access_acceptance_failed') from None
    finally:
        runner.log['finished_at'] = datetime.now(timezone.utc).isoformat()
        runner.log['summary'] = dict(collections.Counter(row['status'] for row in runner.log['requests']))
        runner.save()
    print(json.dumps({'status': runner.log['status'], 'summary': runner.log['summary'],
                      'only_login_and_read_requests': True}))


if __name__ == '__main__':
    main()
