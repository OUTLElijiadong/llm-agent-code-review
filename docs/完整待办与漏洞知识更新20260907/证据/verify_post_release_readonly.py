"""发布后专用数据与安全清单只读验收；正常登录会更新该专用账号会话。"""
import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('acceptance_http', ROOT / 'backend/scripts/verify_permission_acceptance_https.py')
HTTP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HTTP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    assert re.fullmatch('[a-f0-9]{40}', args.expected_release)
    assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() == args.expected_release
    if not args.execute:
        print(json.dumps({'status': 'ready', 'requests_sent': 0}))
        return
    manifest = HTTP.private_json(args.credentials)
    assert manifest['marker'] == '20260907' and manifest['role_code'] == 'qa_permission_20260907'
    assert set(manifest['accounts']) == set(HTTP.ACCOUNTS)
    for account, account_id in zip(HTTP.ACCOUNTS, (103, 104, 105, 106)):
        assert manifest['accounts'][account]['id'] == account_id
        assert manifest['accounts'][account]['username'] == 'qa_20260907_' + account
    runner = HTTP.Runner('https://lijiadong.cn', manifest, None, args.output, phase='post_release_readonly')
    runner.log['expected_release'] = args.expected_release
    try:
        for account in HTTP.ACCOUNTS:
            runner.login(account)
        for account, expected_project in [('owner_a', 162), ('member_a', 162), ('owner_b', 163)]:
            projects = runner.request(account, 'GET', '/api/projects', purpose='重启后隔离与数据持久性')
            runner.check(projects['total'] == 1 and [p['id'] for p in projects['items']] == [expected_project], account + '只可见本主体项目')
        for account, writable in [('owner_a', True), ('member_a', False)]:
            project = runner.request(account, 'GET', '/api/projects/162')
            runner.check(project['id'] == 162 and project['can_update'] is writable and project['can_delete'] is writable, account + '服务端资源权限')
            file = runner.request(account, 'GET', '/api/code-files/1892')
            runner.check(file['project_id'] == 162 and file['file_name'] == 'permission_renamed.py', account + '文件归属与名称持久性')
            runner.check(hashlib.sha256(file['content'].encode()).hexdigest() == '96242d17c7ca31b4c7ee447578b51f3074a002600e77eb9959447f160f665d09', account + '版本二内容持久性')
        runner.request('owner_b', 'GET', '/api/projects/162', 404, purpose='跨主体项目隐藏')
        runner.request('member_a', 'GET', '/api/projects/163', 404, purpose='跨主体项目隐藏')
        runner.request('no_permission', 'GET', '/api/projects/162', 403, purpose='无权限禁止读取真实测试资源')
        catalog = runner.request('member_a', 'GET', '/api/security/checklist', purpose='生产安全清单真实读取')
        expected = json.loads((ROOT / 'backend/app/constants/data/security_catalog.json').read_text())
        runner.log['catalog'] = catalog
        runner.check(catalog['catalog_metadata'] == expected['metadata'], '生产安全清单版本与官方快照一致')
        runner.check(len(catalog['owasp_top10']) == 10, '十个官方分类完整')
        for actual, category in zip(catalog['owasp_top10'], expected['categories']):
            runner.check(all(actual[key] == category[source] for key, source in [('code', 'code'), ('name', 'name_zh'), ('owasp', 'owasp'), ('description', 'name_en'), ('cwe_refs', 'cwe_refs'), ('source_url', 'source_url')]), category['code'] + '名称排序映射与来源一致')
        runner.log['status'] = 'passed'
    except Exception as error:
        runner.log['status'] = 'failed'
        runner.log['failure_type'] = type(error).__name__
        raise
    finally:
        runner.save()
    print(json.dumps({'status': runner.log['status'], 'requests': len(runner.log['requests'])}))


if __name__ == '__main__':
    main()
