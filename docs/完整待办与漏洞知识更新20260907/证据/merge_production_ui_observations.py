"""将主代理真实浏览器观察叠加到独立台账；从不覆盖发布前基线。"""
from __future__ import annotations

import collections
import copy
import csv
import datetime
import hashlib
import io
import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
BASE = EVIDENCE / '追加修复后-全部控件逐角色验收台账.json'
OBSERVATIONS = EVIDENCE / '生产3.8.6真实UI观察.json'
PREFIX = EVIDENCE / '生产3.8.6-全部控件逐角色验收'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict:
    before = sha(BASE)
    assert before == '6d1f6f8a59a4ecd4d56e5b1f7e797c538f717475fb702dc9d855ee7961d98ff8', '发布前基线发生变化，拒绝静默重建'
    base = json.loads(BASE.read_text())
    recorded = json.loads(OBSERVATIONS.read_text())
    ledger = copy.deepcopy(base)
    rows = {row['row_id']: row for row in ledger['controls']}
    assert len(rows) == 3050 == 5 * 610
    assert len({item['observation_id'] for item in recorded['observations']}) == len(recorded['observations'])
    batches = {batch['batch_id']: batch for batch in recorded['source_batches']}
    unmapped = []
    for observation in recorded['observations']:
        assert observation['source_batch_id'] in batches
        assert observation['account_role'] in {account['role'] for account in ledger['accounts']}
        assert observation['status'] in {'passed', 'partially_tested', 'observed', 'blocked', 'pending', 'failed'}
        control = observation.get('control_id')
        if not control:
            unmapped.append(observation)
            continue
        row = rows[f"{observation['account_role']}|{control}"]
        assert row['account_id'] == observation['account_id']
        assert row['account_role'] != 'administrator', '管理员登录未确认，不能升级其基线阻塞'
        row.setdefault('current_release_observations', []).append(observation)
        row['current_release_evidence'].append({
            'type': 'parent_reported_real_browser_observation',
            'file': str(OBSERVATIONS.relative_to(EVIDENCE.parent.parent.parent)),
            'observation_id': observation['observation_id'],
            'source_batch_id': observation['source_batch_id'],
            'observer': '/root',
            'mapper': '/root/permission_matrix',
        })
    for row in rows.values():
        items = row.get('current_release_observations', [])
        completed = [item for item in items if item['status'] != 'pending']
        if not completed:
            continue
        statuses = {item['status'] for item in completed}
        row['current_release_status'] = next(
            status for status in ('failed', 'blocked', 'partially_tested', 'passed', 'observed') if status in statuses
        )
        row['current_release_actual_result'] = '；'.join(item['actual_result'] for item in completed)
        row['current_release_environment'] = recorded['environment']
        row['blocker'] = '；'.join(item['actual_result'] for item in completed if item['status'] == 'blocked') or None
        row['coverage_boundary'] = '仅覆盖所列真实场景；共享节点的其他实例、事件、状态和分支未据此推定通过。'
    statuses = collections.Counter(row['current_release_status'] for row in rows.values())
    ledger['summary']['current_release_statuses'] = dict(statuses)
    ledger['summary']['rows_with_current_observations'] = sum(bool(row.get('current_release_observations')) for row in rows.values())
    ledger['summary']['current_observations'] = len(recorded['observations'])
    ledger['summary']['supplemental_or_unmapped_observations'] = len(unmapped)
    ledger['summary']['note'] += ' 新版 passed 仅表示所列静态控件场景已有最终结果；动态实例或多事件只完成一部分记 partially_tested；隐藏/只读仅观察记 observed；等待最终结果不升级。'
    ledger['production_release'] = recorded['environment']
    ledger['current_observation_map_generated_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ledger['current_source_sha256'] = {BASE.name: before, OBSERVATIONS.name: sha(OBSERVATIONS), Path(__file__).name: sha(Path(__file__))}
    ledger['current_observation_provenance'] = '主代理在真实生产浏览器实测后转录；本子代理只核对源码节点并整理，不冒充第二次浏览器点击。'
    ledger['current_supplemental_observations'] = unmapped
    ledger['global_execution_constraints'] = recorded['global_execution_constraints']
    for account in ledger['accounts']:
        if any(item['account_role'] == account['role'] and item['action'] == '真实登录' and item['status'] == 'passed' for item in recorded['observations']):
            account['login'] = '主代理报告已在生产3.8.6真实登录；见新版观察日志，不使用管理员身份代测'
    json_path = Path(str(PREFIX) + '台账.json')
    json_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')
    columns = ['row_id', 'account_label', 'account_id', 'pages_from_source', 'file', 'line', 'tag', 'label', 'current_release_status', 'blocker', 'current_release_actual_result', 'current_release_evidence', 'coverage_boundary', 'historical_observations']
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows.values():
        writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in columns})
    csv_path = Path(str(PREFIX) + '台账.csv')
    csv_path.write_text('\ufeff' + buffer.getvalue())
    summary = {**ledger['summary'], 'environment': recorded['environment'], 'ledger_sha256': sha(json_path), 'csv_sha256': sha(csv_path), 'base_unchanged': sha(BASE) == before, 'base_sha256': before}
    assert summary['base_unchanged']
    assert sum(statuses.values()) == 3050
    assert statuses['blocked'] >= 610
    Path(str(PREFIX) + '汇总.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    labels = {'passed': '所列场景完成', 'partially_tested': '部分实例或事件已测', 'observed': '仅可见性或状态观察', 'blocked': '阻塞', 'pending': '等待最终结果', 'failed': '实际失败'}
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', '<br>')
    lines = [
        '# 生产 3.8.6 浏览器验收记录', '',
        '环境：https://lijiadong.cn，3.8.6 / 0a14b5359a446ab7b447e10c061b1ebbe6d15e29。主代理实际操作后转录，本子代理核对源码位置并整理；没有操作浏览器或以本地单测代替点击。', '',
        '原发布前 610 × 5 角色台账不变。新版独立 JSON/CSV 位于 `证据/生产3.8.6-全部控件逐角色验收台账.*`，完整原始转录位于 `证据/生产3.8.6真实UI观察.json`。', '',
        '当前逐格状态：' + '；'.join(f'{key}={value}' for key, value in statuses.items()) + '。', '',
        '源码节点可能有多个运行时实例和事件。已测某一实例只覆盖该场景，隐藏入口仅为权限显隐观察；原生关闭按钮、标签和全局快捷键不在模板事件清单中时，保留额外场景，不虚造源码节点。以下细分记录数不等于唯一按钮数。', '',
        '| 观察ID | 账号 | 页面 | 操作 | 实际结果 | 覆盖状态 | 源码控件 |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for item in recorded['observations']:
        lines.append('| ' + ' | '.join(cell(value) for value in [item['observation_id'], str(item['account_id']) + ' / ' + item['account_role'], item['page'], item['action'], item['actual_result'], labels[item['status']], item.get('control_id') or '额外场景或待定位']) + ' |')
    lines.extend(['', '## 当前执行边界', ''])
    lines.extend('- ' + item for item in recorded['global_execution_constraints'])
    (EVIDENCE.parent / '生产3.8.6浏览器验收.md').write_text('\n'.join(lines) + '\n')
    return summary


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
