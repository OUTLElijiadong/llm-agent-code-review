"""只读核对提交、发布账本、实际运行镜像、051 迁移、归因外键与运行文件，不输出环境凭据。"""
import argparse
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path('/opt/code-review')
TABLES = ['agent_response_run', 'agent_team', 'review_task', 'ai_call_log',
          'agent_mesh_message', 'pentest_engagement', 'sandbox_environment']
TARGETS = {'root_agent_run_id': 'agent_response_run', 'agent_run_id': 'agent_response_run',
           'tool_execution_id': 'agent_tool_execution', 'agent_team_id': 'agent_team',
           'agent_team_task_id': 'agent_team_task', 'agent_execution_event_id': 'agent_team_event'}
FILES = ['app/constants/data/security_catalog.json', 'app/constants/data/verified_advisories.json',
         'app/ai/audit_knowledge/known_cves.md', 'app/services/ai_usage_context.py',
         'app/services/sandbox_report_summary.py']
LEDGER_KEYS = {'RELEASE_SHA', 'APP_VERSION', 'BACKEND_RELEASE', 'FRONTEND_RELEASE',
               'BACKEND_IMAGE_ID', 'FRONTEND_IMAGE_ID', 'ALEMBIC_REVISION'}
REVISION = '051_agent_multimodal_assets'


class EvidenceError(Exception):
    """只携固定、可公开的错误码。"""


def require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


def run(command, *, input=None, binary=False):
    try:
        result = subprocess.run(command, input=input, capture_output=True, text=not binary, timeout=45)
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError('readonly_command_timeout') from exc
    require(result.returncode == 0, 'readonly_command_failed')
    return result.stdout


def inspect(command):
    values = json.loads(run(command))
    require(isinstance(values, list) and len(values) == 1, 'inspect_cardinality')
    return values[0]


def read_ledger(raw):
    values = {}
    for line in raw.decode('utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        require(bool(separator), 'ledger_format')
        if key in LEDGER_KEYS:
            require(key not in values, 'ledger_duplicate_key')
            values[key] = value
    require(set(values) == LEDGER_KEYS, 'ledger_missing_key')
    return values


def protected_path(path, protected):
    return any(path == base or path.startswith(base + '/') for base in protected)


def verify_container_filesystem(value, container, protected):
    # 防止运行时旧 bind mount 遮盖镜像源码。assets 命名卷另做逐文件比较。
    for mount in value.get('Mounts', []):
        destination = str(mount.get('Destination', '')).rstrip('/') or '/'
        require(not any(base == destination or base.startswith(destination.rstrip('/') + '/')
                        or destination.startswith(base + '/') for base in protected), 'protected_source_mount')
    for line in run(['docker', 'diff', container]).splitlines():
        _change, separator, path = line.partition(' ')
        require(bool(separator), 'container_diff_format')
        require(not protected_path(path, protected), 'protected_source_modified')


def read_frontend_hashes(directory):
    # 两个固定目录，无外部输入参与 shell。只读哈希，不运行镜像 entrypoint。
    require(directory in ('/opt/prism-dist', '/usr/share/nginx/html'), 'frontend_directory')
    output = run(['docker', 'exec', '-w', directory, 'cr_frontend',
                  'sh', '-c', "find . -type f -exec sha256sum '{}' ';'"])
    hashes = {}
    for line in output.splitlines():
        digest, separator, path = line.partition('  ')
        require(bool(separator) and re.fullmatch('[a-f0-9]{64}', digest) is not None, 'asset_digest_format')
        require(path.startswith('./') and '..' not in Path(path).parts and path not in hashes, 'asset_path')
        hashes[path] = digest
    require('./index.html' in hashes and any(p.startswith('./assets/') for p in hashes), 'frontend_assets_missing')
    return hashes


def collect(expected_release):
    require(re.fullmatch('[a-f0-9]{40}', expected_release) is not None, 'invalid_expected_release')
    require(run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == expected_release, 'checkout_release_mismatch')
    # 绑定不可变提交，工作区 VERSION 或源码即使被改也不能冒充已提交版本。
    version = run(['git', '-C', str(ROOT), 'show', expected_release + ':VERSION']).strip()
    require(re.fullmatch(r'\d+\.\d+\.\d+', version) is not None, 'invalid_commit_version')
    ledger_path = ROOT / 'deploy' / '.releases' / 'current.env'
    ledger_raw = ledger_path.read_bytes()
    ledger = read_ledger(ledger_raw)
    require(ledger['RELEASE_SHA'] == expected_release and ledger['APP_VERSION'] == version, 'ledger_release_mismatch')
    require(ledger['ALEMBIC_REVISION'] == REVISION, 'ledger_revision_mismatch')
    report = {'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'expected_release': expected_release, 'version': version, 'business_writes': False, 'containers': []}
    identities = {}
    for container, image, prefix in [('cr_backend', 'prism-backend', 'BACKEND'), ('cr_frontend', 'prism-frontend', 'FRONTEND')]:
        require(ledger[prefix + '_RELEASE'] == expected_release, 'ledger_component_release_mismatch')
        image_id = ledger[prefix + '_IMAGE_ID']
        require(re.fullmatch('sha256:[a-f0-9]{64}', image_id) is not None, 'ledger_image_id_invalid')
        image_tag = image + ':' + expected_release
        image_value = inspect(['docker', 'image', 'inspect', image_tag])
        value = inspect(['docker', 'inspect', container])
        require(image_value['Id'] == image_id == value['Image'], 'running_image_id_mismatch')
        require(value['Config']['Image'] == image_tag, 'running_image_tag_mismatch')
        labels = image_value.get('Config', {}).get('Labels') or {}
        require(labels.get('org.opencontainers.image.revision', expected_release) == expected_release, 'image_revision_label_conflict')
        require(labels.get('org.opencontainers.image.version', version) == version, 'image_version_label_conflict')
        state = value['State']
        require(state.get('Running') and not state.get('Paused') and not state.get('Restarting')
                and state.get('Health', {}).get('Status') == 'healthy', 'container_not_healthy')
        if container == 'cr_backend':
            env = dict(item.split('=', 1) for item in value['Config']['Env'] if '=' in item)
            require(env.get('APP_RELEASE') == expected_release and env.get('APP_VERSION') == version, 'backend_runtime_version_mismatch')
            verify_container_filesystem(value, container, ['/app/app'])
        else:
            # VITE_APP_* 是构建期变量，nginx final stage 不具有 APP_RELEASE Env。
            verify_container_filesystem(value, container, ['/opt/prism-dist', '/usr/share/nginx/html/index.html'])
        identities[container] = (value['Id'], value['Image'], state['StartedAt'])
        report['containers'].append({'name': container, 'container_id': value['Id'], 'image': image_tag,
                                     'image_id': image_id, 'release': expected_release, 'version': version,
                                     'version_evidence': 'backend_env_and_release_ledger' if container == 'cr_backend' else 'bound_image_and_release_ledger',
                                     'health': 'healthy', 'started_at': state['StartedAt']})
    quoted_tables = ','.join("'" + name + "'" for name in TABLES)
    quoted_columns = ','.join("'" + name + "'" for name in TARGETS)
    sql = '''SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('kind','transaction','schema',DATABASE(),'read_only',@@transaction_read_only);
SELECT JSON_OBJECT('kind','revision','value',version_num) FROM alembic_version;
SELECT JSON_OBJECT('kind','fk','table',k.TABLE_NAME,'column',k.COLUMN_NAME,'target_schema',k.REFERENCED_TABLE_SCHEMA,'target',k.REFERENCED_TABLE_NAME,'target_column',k.REFERENCED_COLUMN_NAME,'delete_rule',r.DELETE_RULE,'source_type',c.COLUMN_TYPE,'nullable',c.IS_NULLABLE='YES','column_count',(SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE k2 WHERE k2.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND k2.TABLE_NAME=k.TABLE_NAME AND k2.CONSTRAINT_NAME=k.CONSTRAINT_NAME))
FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r
ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA=k.CONSTRAINT_SCHEMA AND c.TABLE_NAME=k.TABLE_NAME AND c.COLUMN_NAME=k.COLUMN_NAME
WHERE k.CONSTRAINT_SCHEMA=DATABASE() AND k.TABLE_NAME IN (''' + quoted_tables + ') AND k.COLUMN_NAME IN (' + quoted_columns + ") ORDER BY k.TABLE_NAME,k.COLUMN_NAME;\nROLLBACK;"
    raw = run(['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c',
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database=code_review --batch --raw --skip-column-names'], input=sql)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    transaction, = [row for row in rows if row['kind'] == 'transaction']
    require(transaction['schema'] == 'code_review' and transaction['read_only'] == 1, 'transaction_not_readonly')
    revisions = [row['value'] for row in rows if row['kind'] == 'revision']
    require(revisions == [REVISION], 'alembic_revision_mismatch')
    keys = [row for row in rows if row['kind'] == 'fk']
    require(len(keys) == 42 and {(r['table'], r['column']) for r in keys} == {(t, c) for t in TABLES for c in TARGETS}, 'fk_cardinality')
    require(all(r['target_schema'] == 'code_review' and r['target'] == TARGETS[r['column']]
                and r['target_column'] == 'id' and r['delete_rule'] == 'SET NULL'
                and r['source_type'] == 'bigint' and r['nullable'] == 1 and r['column_count'] == 1 for r in keys), 'fk_contract')
    report.update(alembic_revision=REVISION, readonly_transaction=transaction, foreign_keys=keys)
    source = 'import hashlib,json;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ' + repr(FILES) + '}))'
    hashes = json.loads(run(['docker', 'exec', '-w', '/app', 'cr_backend', 'python', '-c', source]))
    require(set(hashes) == set(FILES), 'running_source_set_mismatch')
    for path, digest in hashes.items():
        committed = run(['git', '-C', str(ROOT), 'show', expected_release + ':backend/' + path], binary=True)
        require(digest == hashlib.sha256(committed).hexdigest(), 'running_source_hash_mismatch')
    report['running_source_sha256'] = hashes
    image_assets = read_frontend_hashes('/opt/prism-dist')
    served_assets = read_frontend_hashes('/usr/share/nginx/html')
    require(all(served_assets.get(path) == digest for path, digest in image_assets.items()), 'served_frontend_hash_mismatch')
    report['frontend_files'] = {'expected_count': len(image_assets), 'served_count': len(served_assets),
                                'all_expected_equal': True, 'index_sha256': image_assets['./index.html'],
                                'manifest_sha256': hashlib.sha256(json.dumps(image_assets, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    # 结束时再次核对账本/HEAD/容器身份，避免取证期间切换容器而拼接两次发布。
    require(ledger_path.read_bytes() == ledger_raw, 'ledger_changed_during_read')
    require(run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == expected_release, 'checkout_changed_during_read')
    for container, identity in identities.items():
        value = inspect(['docker', 'inspect', container])
        require((value['Id'], value['Image'], value['State']['StartedAt']) == identity, 'container_changed_during_read')
        require(value['State'].get('Running') and value['State'].get('Health', {}).get('Status') == 'healthy', 'container_unhealthy_after_read')
    report.update(ledger_sha256=hashlib.sha256(ledger_raw).hexdigest(), status='passed')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-release', required=True)
    args = parser.parse_args()
    try:
        report = collect(args.expected_release)
    except Exception as exc:
        # 不回显命令 stdout/stderr、环境、原始 JSON 或异常 repr。
        print(json.dumps({'status': 'failed', 'business_writes': False,
                          'error_code': str(exc) if isinstance(exc, EvidenceError) else 'unexpected_readonly_failure',
                          'error_type': type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
