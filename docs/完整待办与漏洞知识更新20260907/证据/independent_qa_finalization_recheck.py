"""只读核对 QA 收尾数据与脚本绑定；不再次调用生产 HTTP/数据库。"""
import datetime
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    names = ['生产QA账号收尾执行.json', '生产QA账号收尾执行v2.json', '生产QA收尾v2只读门禁验证.json',
             '生产QA账号收尾脚本与测试指纹.json', '生产QA收尾初始门禁只读诊断.json',
             '生产QA项目名称字符集只读诊断.json']
    read = lambda name: json.loads((EVIDENCE / name).read_text())
    old, done, before, fingerprints, diagnostic, charset = [read(name) for name in names]
    assert old['status'] == 'failed' and old['stage'] == 'read_manifest'
    assert old['error_code'] == 'qa_project_identity_changed' and old['accounts'] == []
    assert done['status'] == 'passed' and done['stage'] == 'finished' and done['remote_process_exit_code'] == 0
    assert done['marker'] == '20260907' and done['disabled_account_ids'] == [103, 104, 105, 106]
    assert before['status'] == 'passed_read_only_preflight' and not before['business_writes']
    assert len(done['accounts']) == len(before['users']) == 4
    versions = []
    for account, initial in zip(done['accounts'], before['users']):
        assert account['id'] == initial['id']
        assert account['status'] == 0 and initial['status'] == 1
        assert account['token_version_before'] == initial['token_version'] + 1
        assert account['token_version_after'] == account['token_version_before'] + 1
        assert account['token_version_delta'] == 1
        assert account['login_http'] == account['before_me_http'] == 200
        assert account['old_token_me_http'] == 403 and account['old_token_me_business_code'] == 40301
        versions.append({key: account[key] for key in ('id', 'token_version_before', 'token_version_after')})
    assert done['model_and_review_counts'] == before['counts'] == diagnostic['counts'] == {'ai_call_log': 0, 'review_task': 0}
    assert done['retained_projects'] == before['projects']
    assert [row['id'] for row in before['projects']] == [162, 163]
    assert [row['user_id'] for row in before['projects']] == [103, 105]
    assert [row['row_sha256'] for row in diagnostic['projects']] == [row['row_sha256'] for row in before['projects']]
    assert all(row['marker_match'] and row['status'] == 'active' for row in before['projects'])
    assert charset['rows'][0] == {'client_charset': 'latin1', 'results_charset': 'latin1', 'connection_charset': 'latin1'}
    for row, suffix in zip(charset['rows'][1:], ('a', 'b')):
        expected = hashlib.sha256(f'QA权限验收-20260907-{suffix}'.encode()).hexdigest()
        assert row['name_sha256'] == expected and row['exact_utf8_name']
    assert done['model_requests'] == 0 and not done['credentials_logged']
    script1 = EVIDENCE / '生产QA账号收尾脚本v1.py'
    script2 = EVIDENCE / '生产QA账号收尾脚本v2.py'
    expected_v2 = script1.read_text().replace('finalize-qa-result.json', 'finalize-qa-result-v2.json').replace(
        'exec mysql --protocol=TCP', 'exec mysql --default-character-set=utf8mb4 --protocol=TCP')
    assert script2.read_text() == expected_v2
    assert old['script_sha256'] == sha(script1) and done['script_sha256'] == sha(script2)
    hashes = {name: sha(EVIDENCE / name) for name in names}
    for item in fingerprints['文件']:
        path = EVIDENCE / item['归档文件']
        assert sha(path) == item['SHA256'] and path.stat().st_size == item['字节数']
        assert Path(item['原始路径']).read_bytes() == path.read_bytes()
        hashes[path.name] = sha(path)
    tests = {}
    for version, expected in [('v1', 21), ('v2', 22)]:
        xml = ET.parse(EVIDENCE / f'生产QA账号收尾本地测试{version}.xml')
        cases = xml.findall('.//testcase')
        assert len(cases) == expected
        assert not any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped'))
        tests[version] = {'tests': len(cases), 'failures': 0, 'errors': 0, 'skipped': 0}
    prepare = ROOT / 'backend/scripts/prepare_permission_acceptance.py'
    assert sha(prepare) == '18d5ccb87620c9366afd185bf4b416aa906bed434a670323ca6c2bd9fc1f03b9'
    auth = ROOT / 'backend/app/core/dependencies.py'
    auth_text = auth.read_text()
    assert auth_text.index('if not user or user.status != 1:') < auth_text.index('if token_version != (user.token_version or 0):')
    hashes['backend/scripts/prepare_permission_acceptance.py'] = sha(prepare)
    hashes['backend/app/core/dependencies.py'] = sha(auth)
    report = {'status': 'passed', 'reviewed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'production_requests': 0, 'production_writes': False, 'tests_rerun': False,
              'disabled_accounts': [103, 104, 105, 106], 'all_status_zero': True,
              'versions': versions, 'login_version_delta': 1, 'disable_version_delta': 1,
              'http_requests_in_execution_evidence': 12, 'normal_logins_200': 4, 'before_me_200': 4,
              'old_token_me_403_40301': 4, 'auth_contract_status_check_precedes_token_version': True,
              'model_and_review_counts': done['model_and_review_counts'],
              'two_project_eight_field_row_hashes_unchanged': True,
              'v1_prewrite_failure_preserved': True, 'v2_only_charset_and_receipt_path_changes': True,
              'archived_xml_recounts': tests,
              'scope': '独立核对现有HTTP与数据库回执及执行源码，不重新登录、禁用或重跑测试；0用量仅指QA四账号范围。',
              'evidence_sha256': hashes, 'reviewer_script_sha256': sha(Path(__file__))}
    (EVIDENCE / '生产QA账号收尾独立数据复核.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'evidence_sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
