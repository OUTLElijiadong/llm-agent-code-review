"""只读重算最终生产 UI 台账；不导入生成器、不请求生产。"""
import collections
import csv
import datetime
import hashlib
import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    names = ['生产3.8.6真实UI观察.json', '生产3.8.6-全部控件逐角色验收台账.json',
             '生产3.8.6-全部控件逐角色验收台账.csv', '生产3.8.6-全部控件逐角色验收汇总.json',
             '追加修复后-全部控件逐角色验收台账.json', '全部按钮与交互源码清单-追加修复后.json',
             '生产3.8.6UI台账冻结核对.json', 'merge_production_ui_observations.py',
             '生产发布运行态与048核验.json']
    before = {name: sha(EVIDENCE / name) for name in names}
    read = lambda name: json.loads((EVIDENCE / name).read_text())
    observed, ledger, summary, base, inventory = [read(name) for name in
        (names[0], names[1], names[3], names[4], names[5])]
    controls = ledger['controls']
    rows = {row['row_id']: row for row in controls}
    original = {row['row_id']: row for row in base['controls']}
    actions = {action['id']: action for action in inventory['actions']}
    assert len(rows) == len(controls) == 3050 and set(rows) == set(original)
    assert len(actions) == inventory['action_count'] == 610
    assert sum(action['button'] for action in actions.values()) == inventory['button_count'] == 446
    assert len(inventory['sources']) == inventory['vue_files'] == 104
    for source in inventory['sources']:
        assert sha(ROOT / 'frontend' / source['file']) == source['sha256']
    roles = {account['role'] for account in ledger['accounts']}
    assert len(roles) == 5
    per_role = {}
    for role in roles:
        current = [row for row in controls if row['account_role'] == role]
        assert len(current) == 610 and {row['control_id'] for row in current} == set(actions)
        per_role[role] = dict(collections.Counter(row['current_release_status'] for row in current))
    for row in controls:
        initial = original[row['row_id']]
        for key in ('row_id', 'account_role', 'account_id', 'control_id', 'file', 'line',
                    'tag', 'label', 'events', 'source_conditions', 'historical_observations'):
            assert row[key] == initial[key]
        action = actions[row['control_id']]
        for key in ('file', 'line', 'tag', 'label', 'events'):
            assert row[key] == action[key]
    assert before[names[4]] == '6d1f6f8a59a4ecd4d56e5b1f7e797c538f717475fb702dc9d855ee7961d98ff8'
    for key, value in ledger['source_sha256'].items():
        path = EVIDENCE.parent / key if key == '生产浏览器验收.md' else ROOT / key
        assert sha(path) == value
    for key, value in ledger['current_source_sha256'].items():
        assert sha(EVIDENCE / key) == value
    assert summary['ledger_sha256'] == before[names[1]] and summary['csv_sha256'] == before[names[2]]
    for key, value in ledger['summary'].items():
        assert summary[key] == value
    statuses = dict(collections.Counter(row['current_release_status'] for row in controls))
    assert statuses == summary['current_release_statuses'] == {
        'untested': 2405, 'blocked': 610, 'passed': 12, 'partially_tested': 11, 'observed': 12}
    assert per_role['administrator'] == {'blocked': 610}
    assert per_role['owner_a'] == per_role['owner_b'] == {'untested': 610}
    assert sum(bool(r['historical_observations']) for r in controls) == 20
    assert sum(len(r['historical_observations']) for r in controls) == 21
    with (EVIDENCE / names[2]).open(encoding='utf-8-sig', newline='') as stream:
        csv_rows = list(csv.DictReader(stream))
    assert len(csv_rows) == 3050 and len({row['row_id'] for row in csv_rows}) == 3050
    for row in csv_rows:
        source = rows[row['row_id']]
        for key, value in row.items():
            expected = source.get(key)
            expected = json.dumps(expected, ensure_ascii=False) if isinstance(expected, (dict, list)) else '' if expected is None else str(expected)
            assert value == expected, (row['row_id'], key)
    observations = {item['observation_id']: item for item in observed['observations']}
    assert len(observations) == len(observed['observations']) == 91
    assert len(observed['source_batches']) == 7
    current_observations = [item for row in controls for item in row.get('current_release_observations', [])]
    supplemental = ledger['current_supplemental_observations']
    assert len(current_observations) + len(supplemental) == len(observations)
    assert len(supplemental) == 24
    assert len({item['observation_id'] for item in current_observations + supplemental}) == 91
    for item in current_observations + supplemental:
        assert item == observations[item['observation_id']]
    batches = {batch['batch_id'] for batch in observed['source_batches']}
    for item in observations.values():
        assert item['source_batch_id'] in batches
    for row in controls:
        current = row.get('current_release_observations', [])
        completed_statuses = {item['status'] for item in current if item['status'] != 'pending'}
        expected_status = next((status for status in ('failed', 'blocked', 'partially_tested', 'passed', 'observed')
                                if status in completed_statuses), original[row['row_id']]['current_release_status'])
        assert row['current_release_status'] == expected_status
        for item in current:
            assert item['control_id'] == row['control_id'] and item['account_id'] == row['account_id']
        if row['current_release_status'] == 'passed':
            assert 'passed' in completed_statuses
    assert sum(bool(row.get('current_release_observations')) for row in controls) == 35
    assert all(sha(EVIDENCE / name) == digest for name, digest in before.items())
    report = {'status': 'passed', 'reviewed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'production_requests': 0, 'production_writes': False, 'tests_rerun': False,
              'source_vue_files': 104, 'controls': 610, 'buttons_or_links': 446,
              'roles': 5, 'rows': 3050, 'statuses': statuses, 'per_role': per_role,
              'json_csv_every_exported_cell_equal': True, 'source_hashes_current': True,
              'source_batches': 7, 'observations': 91, 'observed_rows': 35,
              'supplemental_observations': 24, 'historical_rows': 20, 'historical_observations': 21,
              'base_sha256_unchanged': True,
              'boundaries': ['复核的是主代理真实操作转录与台账映射，本复核未重新点击浏览器。',
                             '12 passed 仅对应所列静态场景；11 partial 仅对应已测动态实例/事件。',
                             '隐藏、只读或未点击链接只算 observed；610管理员阻塞不升级。',
                             '91观察不是91个唯一按钮；完整3050格尚未全部实测。'],
              'evidence_sha256': before, 'reviewer_script_sha256': sha(Path(__file__))}
    (EVIDENCE / '生产UI最终台账独立复核.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'evidence_sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
