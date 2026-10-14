import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

SHA = '1dd46c05db02a1794c459c6b4b0e688c03927fb1'
VERSION = '4.0.101'
BASE = pathlib.Path('/opt/prism-releases') / SHA


def command(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {'exit_code': p.returncode, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()[-1400:]}


def container(name):
    p = command(['docker', 'inspect', name])
    o = json.loads(p['stdout'])[0]
    env = dict(v.split('=', 1) for v in o['Config'].get('Env', []) if '=' in v)
    labels = o['Config'].get('Labels') or {}
    return {'image': o['Config']['Image'], 'image_id': o['Image'], 'state': o['State']['Status'],
            'health': o['State'].get('Health', {}).get('Status'),
            'compose_working_dir': labels.get('com.docker.compose.project.working_dir'),
            'compose_config_files': labels.get('com.docker.compose.project.config_files'),
            'release_env': {k: env.get(k) for k in ['APP_RELEASE', 'APP_VERSION', 'BACKEND_RELEASE', 'FRONTEND_RELEASE']}}


def key_values(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def hashfile(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def get(url):
    try:
        resp = urllib.request.urlopen(url, timeout=20)
    except urllib.error.HTTPError as e:
        resp = e
    data = resp.read()
    meta = {'status': resp.status, 'content_type': resp.headers.get('Content-Type'),
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return meta, data


result = {'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'verification_kind': 'independent_read_only_post_release_strict_script',
          'scope': 'No login, GUI, model invocation or production business writes. Release identity, bundle/backup checks and anonymous HTTP samples only.',
          'expected_sha': SHA, 'expected_version': VERSION,
          'runtime': {n: container(n) for n in ['cr_backend', 'cr_frontend', 'cr_decoy', 'cr_clamav']}}
result['staged_git_head'] = command(['git', '-C', str(BASE), 'rev-parse', 'HEAD'])['stdout']
result['current_ledger'] = key_values(BASE / 'deploy/.releases/current.env')
result['previous_ledger'] = key_values(BASE / 'deploy/.releases/previous.env')
result['default_release_env'] = {k: key_values(BASE / 'deploy/.env').get(k) for k in ['APP_RELEASE', 'APP_VERSION', 'BACKEND_RELEASE', 'FRONTEND_RELEASE']}
result['alembic_current'] = command(['docker', 'exec', 'cr_backend', 'alembic', 'current'])
for route in ['healthz', 'readyz']:
    meta, data = get('https://lijiadong.cn/' + route)
    meta['body'] = json.loads(data)
    result[route] = meta
meta, html = get('https://lijiadong.cn/')
result['served_index'] = meta
assets = re.findall(rb'/assets/buildInfo-[A-Za-z0-9_-]+\.js', html)
if not assets:
    for src in re.findall(rb'<script[^>]+src="([^"]+)"', html):
        _, js = get('https://lijiadong.cn' + src.decode())
        assets = [b'/assets/' + a for a in re.findall(rb'buildInfo-[A-Za-z0-9_-]+\.js', js)]
        if assets:
            break
if assets:
    meta, data = get('https://lijiadong.cn' + assets[0].decode())
    source = data.decode()
    meta.update({'asset': assets[0].decode(), 'version_matches': VERSION in source,
                 'release_matches': SHA in source, 'source': source})
    result['served_build_info'] = meta
template = command(['docker', 'exec', 'cr_frontend', 'cat', '/etc/nginx/templates/default.conf.template'])
result['deployed_template_sha256'] = hashlib.sha256((template['stdout'] + '\n').encode()).hexdigest()
result['nginx_config_test'] = command(['docker', 'exec', 'cr_frontend', 'nginx', '-t'])
bundle = pathlib.Path('/dev/shm') / ('prism-v4.0.101-' + SHA + '.bundle')
result['bundle'] = {'path': str(bundle), 'bytes': bundle.stat().st_size, 'sha256': hashfile(bundle),
                    'expected_bytes': 109567332, 'expected_sha256': '87d2437a63a734bb40be09db899946bbfc8957f91e3e27ab8fa6757f71720d3c'}
bp = pathlib.Path(result['current_ledger']['BACKUP_FILE'])
result['backup'] = {'path': str(bp), 'bytes': bp.stat().st_size, 'sha256': hashfile(bp),
                    'gzip_integrity': command(['gzip', '-t', str(bp)]),
                    'sidecar_sha256': pathlib.Path(str(bp) + '.sha256').read_text().split()[0],
                    'metadata': key_values(pathlib.Path(str(bp) + '.meta'))}
log = pathlib.Path('/var/log/prism-deploy-v4101-1dd46c05.log').read_text(errors='replace')
preflight_marker = f'发布预检通过(target=all, version={VERSION}, sha={SHA})'
attempt_log = log.rsplit(preflight_marker, 1)[-1] if preflight_marker in log else ''
result['deployment_process'] = command(['pgrep', '-f', '^bash ./deploy.sh all --revision ' + SHA + '$'])
restore_receipts = re.findall(r'独立恢复验证通过\(container=cr_testdb, tables=(\d+), alembic=([A-Za-z0-9_]+)\)', attempt_log)
result['restore_success_receipt'] = {'tables': int(restore_receipts[-1][0]), 'alembic': restore_receipts[-1][1]} if restore_receipts else None
result['deploy_log_excerpt'] = [line for line in attempt_log.splitlines() if any(k in line for k in
    ['发布预检通过', '隔离恢复库', '独立恢复验证通过', '发布前备份已完成', 'Alembic 已位于', '发布完成'])]
restore_schemas = re.findall(r'prism_verify_[0-9]+_[0-9]+', attempt_log)
assert restore_schemas, 'Current deploy has no restore schema receipt'
restore_schema = restore_schemas[-1]
result['restore_cleanup'] = command(['docker', 'exec', 'cr_testdb', 'sh', '-ec',
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --batch --skip-column-names -e "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=\'' + restore_schema + '\'"'])
result['restore_schema'] = restore_schema
result['anonymous_http_samples'] = []
for round_number in range(1, 4):
    for name, route, expected in [('unknown_api', '/api/independent-v4101-unknown-route', 404),
                                  ('wrong_method', '/api/auth/login', 405),
                                  ('browser_pending_requires_login', '/api/agent-browser/pending?run_id=987654321&surface=user', 401)]:
        meta, data = get('https://lijiadong.cn' + route)
        try:
            json.loads(data)
            json_valid = True
        except Exception:
            json_valid = False
        meta.update({'round': round_number, 'case': name, 'route': route, 'expected_status': expected,
                     'json_valid': json_valid,
                     'passed': meta['status'] == expected and json_valid and 'application/json' in (meta['content_type'] or '')})
        result['anonymous_http_samples'].append(meta)
result['capacity'] = command(['df', '-B1', '/'])['stdout']
result['remaining_gates'] = ['Authenticated roles, new business writes/readback, persistence and ordinary Safari GUI remain parent-agent acceptance; no Luna production clicking performed.']
result['secrets'] = 'none_recorded'
checks = {
 'release_git': result['staged_git_head'] == SHA,
 'ledger_sha': all(result['current_ledger'].get(k) == SHA for k in ['RELEASE_SHA', 'BACKEND_RELEASE', 'FRONTEND_RELEASE']),
 'ledger_version': result['current_ledger'].get('APP_VERSION') == VERSION,
 'backend_env': result['runtime']['cr_backend']['release_env'] == result['default_release_env'] == {'APP_RELEASE':SHA,'APP_VERSION':VERSION,'BACKEND_RELEASE':SHA,'FRONTEND_RELEASE':SHA},
 'image_tags': result['runtime']['cr_backend']['image'] == 'prism-backend:'+SHA and result['runtime']['cr_frontend']['image'] == 'prism-frontend:'+SHA and result['runtime']['cr_backend']['image_id'] == result['current_ledger'].get('BACKEND_IMAGE_ID') and result['runtime']['cr_frontend']['image_id'] == result['current_ledger'].get('FRONTEND_IMAGE_ID'),
 'compose_path': all(result['runtime'][name]['compose_working_dir'] == str(BASE/'deploy') for name in ['cr_backend','cr_frontend']),
 'alembic': result['alembic_current']['exit_code'] == 0 and '063_agent_browser_actions' in result['alembic_current']['stdout'],
 'health_ready': all(result[route]['status'] == 200 and result[route]['body'].get('release') == SHA and result[route]['body'].get('version') == VERSION for route in ['healthz','readyz']),
 'served_build_info': result.get('served_build_info',{}).get('release_matches') is True and result.get('served_build_info',{}).get('version_matches') is True,
 'nginx_test': result['nginx_config_test']['exit_code'] == 0,
 'template_matches': result['deployed_template_sha256'] == hashfile(BASE/'frontend/nginx.conf.template'),
 'bundle': result['bundle']['bytes'] == result['bundle']['expected_bytes'] and result['bundle']['sha256'] == result['bundle']['expected_sha256'],
 'backup': result['backup']['gzip_integrity']['exit_code'] == 0 and result['backup']['sha256'] == result['backup']['sidecar_sha256'] == result['backup']['metadata']['sha256'],
 'restore_cleanup': result['restore_cleanup']['exit_code'] == 0 and result['restore_cleanup']['stdout'] == '0' and result['restore_success_receipt'] is not None and result['restore_success_receipt']['tables'] == int(result['backup']['metadata']['table_count']) and result['restore_success_receipt']['alembic'] == result['backup']['metadata']['alembic_revision'] == '063_agent_browser_actions',
 'anonymous_samples': all(case['passed'] for case in result['anonymous_http_samples']),
 'healthy_containers': all(result['runtime'][name]['health']=='healthy' for name in ['cr_backend','cr_frontend','cr_decoy','cr_clamav']),
 'deployment_completed': f'发布完成(target=all, sha={SHA}, alembic=063_agent_browser_actions)' in attempt_log and result['deployment_process']['exit_code'] == 1,
}
result['checks'] = checks
result['passed'] = all(checks.values())
print(json.dumps(result, ensure_ascii=False, indent=2))

sys.exit(0 if result["passed"] else 1)
