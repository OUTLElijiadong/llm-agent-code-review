"""Read saved, sanitized receipts only; reconstruct public response bytes without network."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SOURCE = HERE / '生产任务读取错误专项修正后原始结果.json'
OUTPUT = HERE / '生产任务读取错误专项独立复核.json'
IDS = {'owner_a': 107, 'member_a': 108, 'owner_b': 109, 'no_permission': 110}
PERMISSIONS = sorted({
    'project:view', 'project:create', 'project:update', 'project:delete', 'project:member:manage',
    'file:view', 'file:upload', 'file:edit', 'file:delete', 'file:download',
    'review:view', 'review:cancel', 'issue:view', 'issue:handle', 'issue:batch',
    'report:view', 'report:export:json', 'report:export:html', 'security:view', 'agent:view',
})


def encode(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def compare(row, data):
    raw = encode(data)
    assert len(raw) == row['response_bytes'] and sha(raw) == row['response_sha256']
    return {'账号': row['account'], '路径': row['path'], 'HTTP': row['actual'],
            '字节数': len(raw), 'SHA256': sha(raw), '公开信封完全匹配': True}


def main():
    original = SOURCE.read_bytes()
    receipt = json.loads(original)
    assert receipt['status'] == 'passed' and receipt['summary'] == {'passed': 42}
    assert receipt['base_url'] == 'https://lijiadong.cn'
    assert receipt['marker'] == '20260908' and receipt['phase'] == 'task-access'
    assert not receipt['blocked'] and all(a['passed'] is True for a in receipt['assertions'])
    rows = receipt['requests']
    assert len(rows) == 42 and all(r['status'] == 'passed' for r in rows)
    error_rows = rows[12:]
    expected_paths = [('/api/auth/login', 'POST'), ('roles', 'GET'), ('permissions', 'GET')]
    for index, (account, uid) in enumerate(IDS.items()):
        for j, (path, method) in enumerate(expected_paths):
            row = rows[index * 3 + j]
            path = path if j == 0 else f'/api/rbac/users/{uid}/{path}'
            assert (row['account'], row['method'], row['path'], row['actual'], row['expected']) == (account, method, path, 200, 200)
    plan = [(account, f'/api/review/tasks/{tid}{suffix}')
            for account in ['anonymous', 'no_permission', 'owner_a', 'member_a', 'owner_b']
            for tid in [-1, 161, 162] for suffix in ['', '/issues']]
    assert [(r['account'], r['path']) for r in error_rows] == plan
    verified = []
    for row in error_rows:
        assert row['method'] == 'GET'
        rid = row['response_request_id']
        assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}', rid)
        if row['account'] == 'anonymous':
            status, code, message, action = 401, 40100, '缺少token', '请重新登录后再试'
        elif row['account'] == 'no_permission':
            permission = 'issue:view' if row['path'].endswith('/issues') else 'review:view'
            status, code, message, action = 403, 40303, f'无操作权限: 需要 {permission}', '请联系管理员确认权限'
        else:
            status, code = 404, 40400
            message = '审查任务不存在或当前账号无权访问'
            action = '请返回审查记录列表重新选择；如需访问，请联系项目负责人确认权限'
        assert row['actual'] == status and row['expected'] == status
        # JSONResponse's actual production insertion order, with no private detail field.
        data = {'code': code, 'message': message, 'request_id': rid,
                'retryable': False, 'next_action': action}
        verified.append(compare(row, data))
    counts = Counter(row['actual'] for row in error_rows)
    assert counts == {401: 6, 403: 6, 404: 18}
    positives = []
    for row in rows[:12]:
        if row['path'].endswith('/permissions') or (row['account'] == 'no_permission' and row['path'].endswith('/roles')):
            data = [] if row['account'] == 'no_permission' else PERMISSIONS
            positives.append(compare(row, {'code': 0, 'message': 'ok', 'data': data}))
    assert len(positives) == 5
    assert len(set(r['response_request_id'] for r in error_rows)) == 30
    forbidden_keys = {'password', 'authorization', 'access_token', 'token', 'body', 'response_body', 'detail'}
    def no_private_fields(obj):
        if isinstance(obj, dict):
            assert not {str(k).lower() for k in obj} & forbidden_keys
            for value in obj.values():
                no_private_fields(value)
        elif isinstance(obj, list):
            for value in obj:
                no_private_fields(value)
    no_private_fields(receipt)
    source_paths = [
        'backend/app/core/error_handlers.py', 'backend/app/core/exceptions.py',
        'backend/app/core/dependencies.py', 'backend/app/core/rbac_dependency.py',
        'backend/app/services/review_service.py', 'backend/app/api/v1/rbac.py',
        'backend/app/schemas/common.py', 'frontend/nginx.conf.template',
    ]
    result = {
        '核验时间UTC': datetime.now(timezone.utc).isoformat(),
        '结论': '42请求结构及结果通过；30错误公开信封和5权限/空角色响应独立重建SHA全部一致',
        '原始收据': {'文件': SOURCE.name, 'SHA256': sha(original)},
        '重算脚本SHA256': sha(Path(__file__).read_bytes()),
        '来源SHA256': {name: sha((ROOT / name).read_bytes()) for name in source_paths},
        '请求数': 42, '正常登录与自身RBAC读取': 12,
        '错误HTTP计数': dict(counts), '错误信封SHA匹配数': len(verified),
        '权限与空角色SHA匹配数': len(positives),
        '头体同服务器编号': True, '边缘覆盖客户端编号数': sum(r['request_id'] != r['response_request_id'] for r in error_rows),
        '错误响应仅五个公开字段': True, '未返回detail或任务/项目正文': True,
        '错误响应逐条比对': verified, '权限与空角色逐条比对': positives,
        '方法': '按真实生产JSONResponse字段顺序、UTF8及紧凑JSON编码，从固定公开合同与响应头编号重建预期字节；逐条对照原收据SHA和长度，不读取原响应正文',
        '边界': '4个成功登录正文含token、3个专用角色正文含动态时间，未重建这7个正文；身份与角色通过原Runner已审实现及已保存11项断言支持，不把断言冒充独立正文重算。模型/任务新增量另由生产数据库收尾快照核查。',
        '生产请求': 0, '生产凭据读取': False, '应用修改': False,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'error_sha_matches': 30, 'permission_sha_matches': 5, 'total_requests': 42}))


if __name__ == '__main__':
    main()
