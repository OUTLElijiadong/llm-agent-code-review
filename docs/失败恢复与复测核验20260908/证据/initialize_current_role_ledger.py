"""Initialize current-role execution ledger from the existing source/route exporter.

Previous QA identities are inactive. Previous evidence remains historical; it cannot
make any current-release row pass. No network or application writes are performed.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
import csv
import hashlib
import json

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parent
INVENTORY = BASE / '全部按钮与交互源码清单-最终绑定.json'
POSITIONING = BASE / '最终源码角色定位-台账.json'
PREVIOUS = ROOT / 'docs/完整待办与漏洞知识更新20260907/证据/生产3.8.6-全部控件逐角色验收台账.json'
OUTPUT = BASE / '本轮全部控件逐角色验收台账.json'
if OUTPUT.exists():
    raise RuntimeError('输出已存在，不覆盖任何本轮或历史验收记录')


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(row):
    return (row['account_role'], row['file'], row['tag'], row['label'],
            json.dumps(row['events'], sort_keys=True, ensure_ascii=False))


inventory, positioning, previous = read(INVENTORY), read(POSITIONING), read(PREVIOUS)
for source in inventory['sources']:
    assert sha(ROOT / 'frontend' / source['file']) == source['sha256']
old_by_key = defaultdict(list)
for row in previous['controls']:
    if row.get('current_release_observations'):
        old_by_key[identity(row)].append(row)
new_key_counts = Counter(identity(row) for row in positioning['controls'])
rows = []
for source in positioning['controls']:
    row = deepcopy(source)
    row['previous_acceptance_account_id'] = row['account_id']
    row['account_id'] = None
    row['project_scope'] = '待主代理新建20260908标记的隔离验收项目；实际项目ID与资源关系以真实返回为准'
    row['current_release_status'] = 'blocked' if row['account_role'] == 'administrator' else 'untested'
    row['blocker'] = '待用户正常管理员生产登录' if row['account_role'] == 'administrator' else None
    row['execution_prerequisite'] = '等待本轮最小权限专用账号及有效会话；旧QA103-106已停用，不复用或扩权'
    row['current_release_actual_result'] = None
    row['current_release_evidence'] = []
    row['previous_release_observations'] = []
    old = old_by_key[identity(row)]
    if len(old) == 1 and new_key_counts[identity(row)] == 1:
        prior = old[0]
        row['previous_release_observations'] = [{
            'source_file': str(PREVIOUS.relative_to(ROOT)), 'source_row_id': prior['row_id'],
            'environment': prior['current_release_environment'],
            'status_in_previous_release': prior['current_release_status'],
            'observations': prior['current_release_observations'],
            'evidence': prior['current_release_evidence'],
            'boundary': '仅按角色/源码文件/标签/文字/事件唯一关联的历史场景；不表示本轮行为通过。',
        }]
    rows.append(row)
accounts = deepcopy(positioning['accounts'])
for account in accounts:
    account['previous_acceptance_id'] = account.pop('id')
    account['id'] = None
    account['login'] = '尚无本轮已核验的生产身份会话'
    account['scope'] = '本轮实际账号与项目归属待真实返回；角色定义沿用隔离矩阵'
summary = {
    'source_actions': inventory['action_count'], 'source_buttons_or_links': inventory['button_count'],
    'roles': len(accounts), 'total_role_control_rows': len(rows),
    'current_release_statuses': dict(Counter(row['current_release_status'] for row in rows)),
    'current_release_passed': 0,
    'rows_with_previous_release_observations': sum(bool(row['previous_release_observations']) for row in rows),
    'previous_release_observations': sum(len(group['observations']) for row in rows for group in row['previous_release_observations']),
    'note': '源码节点乘角色；动态实例和未测场景不由源码或本地挂载测试推定通过。',
}
assert len(rows) == 5 * inventory['action_count']
assert len({row['row_id'] for row in rows}) == len(rows)
assert all(row['current_release_actual_result'] is None and not row['current_release_evidence'] for row in rows)
out = {
    'summary': summary, 'accounts': accounts, 'controls': rows,
    'source_sha256': {str(path.relative_to(ROOT)): sha(path) for path in (INVENTORY, POSITIONING, PREVIOUS, Path(__file__))},
    'application_sources': inventory['sources'],
    'previous_supplemental_observations': previous.get('current_supplemental_observations', []),
    'boundary': '仅初始化本轮生产待测台账；旧版记录不覆盖，旧QA不启用；本地故障注入另见专项XML。',
}
OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
columns = list(rows[0])
with OUTPUT.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
(BASE / '本轮全部控件逐角色汇总.json').write_text(json.dumps({**summary, 'ledger_sha256': sha(OUTPUT)}, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False))
