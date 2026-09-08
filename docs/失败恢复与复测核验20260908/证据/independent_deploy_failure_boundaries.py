#!/usr/bin/env python3
"""独立审查真实部署脚本；仅复用 test_scripts.sh 输出的本地无网络命令替身。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--fixtures', type=Path, required=True, help='父矩阵 test_artifacts 目录')
parser.add_argument('--source', type=Path, default=Path.cwd(), help='待审仓库；执行前冻结脚本字节')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = Path(tempfile.mkdtemp(prefix='prism-independent-boundaries-'))
source_paths = ['deploy/deploy.sh', 'deploy/rollback.sh', 'deploy/lib/common.sh']
source_bytes = {name: (args.source / name).read_bytes() for name in source_paths}
source_sha = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
report = {
    'scope': 'isolated real deploy/rollback with local command substitutes',
    'production_requests': 0, 'database_mutations': 0, 'model_requests': 0,
    'source_sha256': source_sha, 'fixture_root': str(args.fixtures), 'cases': [],
}
cases = [
    ('single_lock_release', 'backup'), ('rollback_final_ps', 'backend_up'),
    ('asset_original_exit', 'assets'), ('explicit_fatal', 'none'),
    ('migration_verify_fatal', 'none'), ('release_final_ps', 'none'),
]
for name, scenario in cases:
    template = args.fixtures / ('failure-matrix-' + (scenario if scenario != 'none' else 'backup'))
    workspace = root / name
    shutil.copytree(template, workspace)
    for file in workspace.rglob('*'):
        if file.is_file() and file.suffix != '.gz':
            data = file.read_bytes()
            if str(template).encode() in data:
                file.write_bytes(data.replace(str(template).encode(), str(workspace).encode()))
    for relative, data in source_bytes.items():
        (workspace / 'repo' / relative).write_bytes(data)
    shutil.copy2(workspace / 'original.env', workspace / 'releases/current.env')
    for state in ['previous.env', 'pending.env']:
        (workspace / 'releases' / state).unlink(missing_ok=True)
    (workspace / 'docker.log').write_text('')
    if name == 'single_lock_release':
        shim = workspace / 'bin/rmdir'
        shim.write_text('''#!/usr/bin/env bash
if [[ "$1" == "$MAINTENANCE_LOCK_DIR" ]]; then
  printf 'release\n' >> "$FAKE_RELEASE_WORKSPACE/lock-release.log"
  if [[ ! -f "$FAKE_RELEASE_WORKSPACE/first-release" ]]; then
    /bin/rmdir "$1" || exit $?
    /bin/mkdir "$1"
    touch "$FAKE_RELEASE_WORKSPACE/first-release"
    exit 0
  fi
fi
exec /bin/rmdir "$@"
''')
        shim.chmod(0o755)
    injection = ''
    if name in {'rollback_final_ps', 'release_final_ps'}:
        target = 'a' * 40 if name == 'rollback_final_ps' else 'b' * 40
        injection = f'''if [[ "${{APP_RELEASE:-}}" == {target} && "$*" == 'compose ps' ]]; then
  printf 'injected:final_ps\n' >> "$FAKE_DOCKER_LOG"; exit 46
fi
'''
    elif name == 'migration_verify_fatal':
        injection = '''if [[ "${APP_RELEASE:-}" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb && "${1:-}" == run && "${*: -1}" == current ]]; then
  printf 'injected:migration_verify\n' >> "$FAKE_DOCKER_LOG"
  printf '047_review_input_snapshot\n'; exit 0
fi
'''
    if injection:
        wrapper = workspace / 'bin/docker'
        wrapper.write_text(wrapper.read_text().replace('set -eu', 'set -eu\n' + injection, 1))
    env = dict(os.environ, PATH=f'{workspace}/bin:' + os.environ['PATH'],
        FAIL_SCENARIO=scenario, FAKE_RELEASE_SHA='a' * 40, FAKE_RELEASE_VERSION='3.8.2',
        FAKE_ALEMBIC_REVISION='048_ai_usage_attribution', FAKE_RELEASE_WORKSPACE=str(workspace),
        FAKE_DOCKER_LOG=str(workspace / 'docker.log'), RELEASE_STATE_DIR=str(workspace / 'releases'),
        MAINTENANCE_LOCK_DIR=str(workspace / 'lock'), DEPLOY_ENV_FILE='.env',
        BACKEND_HEALTH_TIMEOUT='1', FRONTEND_HEALTH_TIMEOUT='1')
    if name == 'explicit_fatal':
        env['FAKE_RELEASE_MODE'] = 'invalid_heads'
    process = subprocess.run([str(workspace / 'repo/deploy/deploy.sh'), 'all', '--revision', 'b' * 40],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    log = process.stdout
    output_log = workspace / 'independent-output.log'
    output_log.write_text(log)
    expected_rc = {'single_lock_release': 41, 'rollback_final_ps': 44, 'asset_original_exit': 43,
        'explicit_fatal': 1, 'migration_verify_fatal': 1, 'release_final_ps': 0}[name]
    checks = {'original_exit': process.returncode == expected_rc,
        'no_database_restore': not any(s in (workspace / 'docker.log').read_text() for s in ['downgrade', 'restore']),
        'one_failure_or_success': log.count('发布事务失败(rc=') == (0 if name == 'release_final_ps' else 1)}
    if name == 'single_lock_release':
        checks.update(release_once=(workspace / 'lock-release.log').read_text().splitlines() == ['release'],
            simulated_next_owner_lock_survives=(workspace / 'lock').is_dir())
    else:
        checks['lock_released'] = not (workspace / 'lock').exists()
    if name in {'rollback_final_ps', 'asset_original_exit'}:
        checks.update(rollback_succeeded='应用自动回滚完成' in log,
            no_false_failure='应用自动回滚失败' not in log,
            pending_removed=not (workspace / 'releases/pending.env').exists())
    if name in {'explicit_fatal', 'migration_verify_fatal'}:
        stage = 'migration_preflight' if name == 'explicit_fatal' else 'migration_verify'
        checks.update(stage_feedback=f'stage={stage},' in log,
            application_not_switched='应用尚未切换' in log,
            pending_retained=(workspace / 'releases/pending.env').exists(),
            current_unchanged=(workspace / 'releases/current.env').read_bytes() == (workspace / 'original.env').read_bytes())
        if name == 'migration_verify_fatal':
            checks['migration_boundary_explicit'] = '数据库迁移已尝试' in log
    if name == 'release_final_ps':
        checks.update(release_committed=('RELEASE_SHA=' + 'b' * 40) in (workspace / 'releases/current.env').read_text(),
            success_feedback='发布完成(target=' in log,
            display_warning='容器列表读取失败' in log,
            no_rollback='开始应用层回滚' not in log)
    report['cases'].append({'name': name, 'exit': process.returncode, 'expected_exit': expected_rc,
        'checks': checks, 'passed': all(checks.values()), 'log': str(output_log),
        'log_sha256': hashlib.sha256(output_log.read_bytes()).hexdigest(),
        'selected_events': [line for line in log.splitlines() if any(s in line for s in
            ['发布事务失败', '回滚完成', '回滚失败', '尚未切换', '迁移已尝试', '容器列表读取失败', '发布完成(target='])]})
report['source_unchanged'] = all((args.source / name).read_bytes() == data for name, data in source_bytes.items())
report['passed'] = report['source_unchanged'] and all(case['passed'] for case in report['cases'])
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'passed': report['passed'], 'source_unchanged': report['source_unchanged'],
    'cases': [{'name': c['name'], 'exit': c['exit'], 'passed': c['passed'], 'failed_checks': [k for k,v in c['checks'].items() if not v]} for c in report['cases']]}, ensure_ascii=False, indent=2))
raise SystemExit(0 if report['passed'] else 1)
