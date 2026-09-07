"""补采公开前端资源逐路径摘要与本轮备份白名单元数据；不读取 SQL 正文。"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


REMOTE_CODE = r'''
def hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def parse_unique(raw):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        require(bool(separator) and key not in result, 'metadata_format')
        result[key] = value
    return result


def collect_extra():
    observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
    require(run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == EXPECTED_RELEASE, 'checkout_release_mismatch')
    ledger_path = ROOT / 'deploy' / '.releases' / 'current.env'
    ledger_raw = ledger_path.read_bytes()
    ledger = read_ledger(ledger_raw)
    require(ledger['RELEASE_SHA'] == EXPECTED_RELEASE and ledger['FRONTEND_RELEASE'] == EXPECTED_RELEASE, 'release_mismatch')
    image_tag = 'prism-frontend:' + EXPECTED_RELEASE
    image_value = inspect(['docker', 'image', 'inspect', image_tag])
    container = inspect(['docker', 'inspect', 'cr_frontend'])
    require(container['Config']['Image'] == image_tag and image_value['Id'] == container['Image'] == ledger['FRONTEND_IMAGE_ID'], 'image_binding_mismatch')
    state = container['State']
    require(state['Running'] and state.get('Health', {}).get('Status') == 'healthy', 'frontend_not_healthy')
    identity = (container['Id'], container['Image'], state['StartedAt'])
    verify_container_filesystem(container, 'cr_frontend', ['/opt/prism-dist', '/usr/share/nginx/html/index.html'])
    expected = read_frontend_hashes('/opt/prism-dist')
    served_all = read_frontend_hashes('/usr/share/nginx/html')
    require(all(served_all.get(path) == digest for path, digest in expected.items()), 'served_frontend_hash_mismatch')
    served = {path: served_all[path] for path in sorted(expected)}

    ledger_extended = parse_unique(ledger_raw)
    backup_ref = ledger_extended.get('BACKUP_FILE', '')
    require(bool(backup_ref) and backup_ref != 'none', 'release_backup_missing')
    backup = Path(backup_ref)
    if not backup.is_absolute():
        backup = ROOT / 'deploy' / backup
    backup = backup.resolve(strict=True)
    require(backup.parent == ROOT / 'backups', 'backup_path_outside_expected_directory')
    require(re.fullmatch(r'code_review_\d{8}T\d{6}Z_[a-f0-9]{12}\.sql\.gz', backup.name) is not None, 'backup_filename')
    meta_path = Path(str(backup) + '.meta')
    checksum_path = Path(str(backup) + '.sha256')
    require(not meta_path.is_symlink() and not checksum_path.is_symlink(), 'backup_metadata_symlink')
    meta_raw = meta_path.read_bytes()
    checksum_raw = checksum_path.read_bytes()
    meta = parse_unique(meta_raw)
    allowed = {'created_at_utc', 'reason', 'git_sha', 'alembic_revision', 'table_count', 'sha256', 'file'}
    require(set(meta) == allowed, 'backup_meta_fields')
    require(meta['reason'] == 'pre_deploy', 'backup_reason_not_pre_deploy')
    require(re.fullmatch(r'\d{8}T\d{6}Z', meta['created_at_utc']) is not None, 'backup_time_format')
    require(re.fullmatch('[a-f0-9]{40}', meta['git_sha']) is not None, 'backup_git_sha_format')
    require(re.fullmatch(r'[a-zA-Z0-9_]{1,80}', meta['alembic_revision']) is not None, 'backup_revision_format')
    require(meta['table_count'].isdigit() and int(meta['table_count']) > 0, 'backup_table_count')
    require(meta['file'] == backup.name and re.fullmatch('[a-f0-9]{64}', meta['sha256']) is not None, 'backup_meta_file_sha')
    backup_stat = backup.stat()
    actual_sha = hash_file(backup)
    require(actual_sha == meta['sha256'], 'backup_file_sha_mismatch')
    require(checksum_raw.decode('utf-8').strip() == actual_sha + '  ' + backup.name, 'backup_sidecar_sha_mismatch')
    after_stat = backup.stat()
    require((after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns) ==
            (backup_stat.st_ino, backup_stat.st_size, backup_stat.st_mtime_ns)
            and meta_path.read_bytes() == meta_raw and checksum_path.read_bytes() == checksum_raw, 'backup_changed_during_read')
    require(ledger_path.read_bytes() == ledger_raw, 'ledger_changed_during_read')
    require(run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == EXPECTED_RELEASE, 'checkout_changed_during_read')
    after = inspect(['docker', 'inspect', 'cr_frontend'])
    require((after['Id'], after['Image'], after['State']['StartedAt']) == identity, 'frontend_changed_during_read')
    require(after['State']['Running'] and after['State'].get('Health', {}).get('Status') == 'healthy', 'frontend_unhealthy_after_read')
    return {
        'status': 'passed', 'observed_at_utc': observed,
        'completed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'release': EXPECTED_RELEASE, 'version': ledger['APP_VERSION'],
        'production_writes': False, 'sql_body_read': False, 'backup_rerun': False, 'migration_rerun': False,
        'collector_sha256': COLLECTOR_SHA, 'reused_helper_sha256': HELPER_SHA,
        'ledger_sha256': hashlib.sha256(ledger_raw).hexdigest(),
        'frontend': {
            'container_id': identity[0], 'image_id': identity[1], 'started_at': identity[2],
            'image_tag': image_tag, 'expected_directory': '/opt/prism-dist', 'served_directory': '/usr/share/nginx/html',
            'expected_count': len(expected), 'served_total_count': len(served_all),
            'expected_sha256': dict(sorted(expected.items())), 'served_expected_sha256': served,
            'manifest_sha256': hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'served_total_manifest_sha256': hashlib.sha256(json.dumps(served_all, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'index_sha256': expected['./index.html'], 'all_expected_equal': True,
        },
        'backup': {
            'file': backup.name, 'directory_scope': '/opt/code-review/backups',
            'release_ledger_backup_file_matches': True, 'bytes': backup_stat.st_size,
            'sha256': actual_sha, 'metadata_sha256': hashlib.sha256(meta_raw).hexdigest(),
            'checksum_sidecar_sha256': hashlib.sha256(checksum_raw).hexdigest(),
            'metadata': {key: (int(meta[key]) if key == 'table_count' else meta[key]) for key in sorted(allowed)},
            'actual_file_meta_and_sidecar_sha_equal': True,
            'restore_rerun': False, 'gzip_test_rerun': False,
        },
    }


try:
    print(json.dumps(collect_extra(), ensure_ascii=False, indent=2))
except Exception as exc:
    print(json.dumps({'status': 'failed', 'production_writes': False,
                      'error_code': str(exc) if isinstance(exc, EvidenceError) else 'unexpected_readonly_failure',
                      'error_type': type(exc).__name__}, ensure_ascii=False))
    raise SystemExit(1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve()
    helper = here.with_name('collect_release_readonly.py').read_bytes()
    prefix = '\n'.join([
        'EXPECTED_RELEASE = ' + repr(args.expected_release),
        'HELPER_SHA = ' + repr(hashlib.sha256(helper).hexdigest()),
        'COLLECTOR_SHA = ' + repr(hashlib.sha256(here.read_bytes()).hexdigest()),
    ])
    source = prefix + '\n' + helper.decode().split('\ndef main():', 1)[0] + '\n' + REMOTE_CODE
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'root@81.70.251.90', 'python3', '-'],
                            input=source, capture_output=True, text=True, timeout=300)
    report = json.loads(result.stdout)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'output': str(args.output), 'returncode': result.returncode}, ensure_ascii=False))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
