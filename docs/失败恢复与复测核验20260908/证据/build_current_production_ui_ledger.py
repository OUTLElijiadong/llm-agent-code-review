#!/usr/bin/env python3
"""离线叠加四账号真实观察；保留初始化和历史，不发生产请求。"""
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RELEASE = '4035d1902342ab88357b5bb6071545d1a0572dd1'
BASE = HERE / '本轮全部控件逐角色验收台账.json'
INVENTORY = HERE / '全部按钮与交互源码清单-最终绑定.json'
OUTPUT = HERE / '生产3.8.7-全部控件逐角色验收台账.json'
SOURCES = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    raw = path.read_bytes()
    SOURCES[str(path.relative_to(ROOT))] = sha(raw)
    return json.loads(raw)


base = load(BASE)
inventory = load(INVENTORY)
supplement = load(HERE / '生产浏览器精确动作补充.json')
accounts = {'owner_a': 107, 'member_a': 108, 'owner_b': 109, 'no_permission': 110}
groups = {role: load(HERE / f'生产浏览器{role}观察.json') for role in accounts}
for role, group in groups.items():
    assert group['account'] == role and group['account_id'] == accounts[role]
    assert group['version'] == '3.8.7' and group['release'] == RELEASE
    assert len({item['id'] for item in group['observations']}) == len(group['observations'])
assert [len(groups[role]['observations']) for role in accounts] == [17, 7, 6, 6]
assert inventory['vue_files'] == len(inventory['sources']) == 104
assert inventory['action_count'] == len(inventory['actions']) == 616
assert inventory['button_count'] == sum(row['is_button_or_link'] for row in base['controls'] if row['account_role'] == 'owner_a') == 452
for source in inventory['sources']:
    assert sha((ROOT / 'frontend' / source['file']).read_bytes()) == source['sha256']
assert Counter(row['current_release_status'] for row in base['controls']) == {'untested': 2464, 'blocked': 616}
assert all(row['account_id'] is None for row in base['controls'])

# 逐条人工读原始观察及模板后确定映射；动态行只代表实际 QA 实例。
# 元组为账号、原观察ID、唯一源码节点、状态、所核对象与未覆盖边界。
SPEC = [
    ('owner_a', 'ui01', 'src/views/auth/Login.vue:205:315', 'passed', '补充精确点击登录按钮，正常登录成功；不含错误、冷却或断网状态'),
    ('owner_a', 'ui03', 'src/views/dashboard/Dashboard.vue:20:339', 'partially_tested', '有点击事实，未单独捕获刷新结束结果'),
    ('owner_a', 'ui04', 'src/components/layout/AppHeader.vue:231:126', 'passed', '打开功能导航弹窗'),
    ('owner_a', 'ui06', 'src/components/layout/AppHeader.vue:285:130', 'partially_tested', '只点击项目管理这一动态搜索结果，跳转与弹窗关闭均观察到'),
    ('owner_a', 'ui08', 'src/views/project/ProjectList.vue:38:454', 'passed', '非空项目列表顶部新建项目按钮打开表单；未提交'),
    ('owner_a', 'ui08', 'src/views/project/ProjectForm.vue:123:450', 'observed', '空名称的创建项目按钮为禁用，仅状态观察'),
    ('owner_a', 'ui09', 'src/views/project/ProjectForm.vue:122:449', 'passed', '新建表单取消后回到列表'),
    ('owner_a', 'ui10', 'src/views/project/ProjectList.vue:193:466', 'partially_tested', '表格QA-a编辑按钮；紧连首次点击未打开，稳定后重试打开，不将全部时序判通过'),
    ('owner_a', 'ui11', 'src/views/project/ProjectForm.vue:122:449', 'partially_tested', '编辑表单取消点击已执行，未完整捕获紧随结果；不能覆盖新建取消结果冒充该分支通过'),
    ('owner_a', 'ui12', 'src/views/project/ProjectList.vue:192:465', 'partially_tested', '补充精确表格详情按钮，只覆盖项目164这一动态行'),
    ('owner_a', 'ui14', 'src/views/code/CodeFileList.vue:89:329', 'partially_tested', '只覆盖项目164/file1894文本文件查看代码并进入编辑器'),
    ('owner_a', 'ui16', 'src/views/review/ReviewTaskDetail.vue:17:533', 'passed', '404详情重新加载保持准确错误并更新请求编号，不伪报成功'),
    ('owner_a', 'ui17', 'src/views/review/ReviewTaskDetail.vue:20:534', 'passed', '从404状态返回审查记录，实际到列表'),
    ('member_a', 'm01', 'src/views/auth/Login.vue:205:315', 'passed', '补充精确点击登录按钮，正常成员登录成功'),
    ('member_a', 'm02', 'src/views/project/ProjectList.vue:38:454', 'observed', '顶部新建项目入口仍可见，未点击'),
    ('member_a', 'm02', 'src/views/project/ProjectList.vue:193:466', 'observed', '当前项目表格行编辑入口隐藏，未点击'),
    ('member_a', 'm02', 'src/views/project/ProjectList.vue:194:467', 'observed', '当前项目表格行删除入口隐藏，未点击'),
    ('member_a', 'm03', 'src/views/project/ProjectList.vue:192:465', 'partially_tested', '补充精确表格详情按钮，只覆盖成员项目164'),
    ('member_a', 'm04', 'src/views/project/ProjectDetail.vue:244:436', 'observed', '成员页添加成员按钮隐藏；标签操作本身不在616节点中'),
    ('member_a', 'm04', 'src/views/project/ProjectDetail.vue:286:437', 'observed', '成员角色操作列为—，未呈现角色变更选择器'),
    ('member_a', 'm04', 'src/views/project/ProjectDetail.vue:296:438', 'observed', '成员移除操作隐藏，未执行移除'),
    ('member_a', 'm05', 'src/views/code/CodeFileList.vue:90:330', 'observed', '文件1894版本历史入口可见，未点击；未将下载归入二进制下载按钮'),
    ('member_a', 'm06', 'src/views/code/CodeFileList.vue:89:329', 'partially_tested', '只覆盖项目164/file1894文本文件查看代码并导航成功'),
    ('member_a', 'm06', 'src/views/code/CodeEditor.vue:69:324', 'observed', '保存按钮隐藏；没有实际编辑文本，不据AX settable判断输入只读'),
    ('member_a', 'm07', 'src/components/layout/AppHeader.vue:256:129', 'passed', '点击退出并确认；原观察明确下一次正常登录证明返回登录页'),
    ('owner_b', 'b01', 'src/views/auth/Login.vue:205:315', 'passed', '补充精确点击登录按钮，另一项目负责人正常登录成功'),
    ('owner_b', 'b04', 'src/views/project/ProjectDetail.vue:56:429', 'passed', '无权项目错误页重试读取后仍拒绝且不显示旧数据'),
    ('owner_b', 'b05', 'src/views/project/ProjectDetail.vue:4:424', 'passed', '无权项目页返回入口实际导航到项目列表'),
    ('owner_b', 'b06', 'src/components/layout/AppHeader.vue:256:129', 'passed', '菜单退出并确认，原记录报告正常退出以便下个账号登录'),
    ('no_permission', 'n01', 'src/views/auth/Login.vue:205:315', 'passed', '补充精确点击登录按钮，无权限账号正常登录成功'),
    ('no_permission', 'n02', 'src/views/dashboard/Dashboard.vue:21:340', 'observed', '导出统计报告入口隐藏；没有执行导出'),
    ('no_permission', 'n03', 'src/views/dashboard/Dashboard.vue:20:339', 'passed', '刷新返回5/5项、自身零数据且未暴露其他账号'),
    ('no_permission', 'n04', 'src/views/error/Forbidden.vue:22:348', 'observed', '403页返回首页按钮可见，该条本身未点击'),
    ('no_permission', 'n04', 'src/views/error/Forbidden.vue:23:349', 'observed', '403页返回上一页按钮仅可见，未点击'),
    ('no_permission', 'n05', 'src/views/error/Forbidden.vue:22:348', 'passed', '实际从403返回工作台，仍零数据'),
    ('no_permission', 'n06', 'src/components/layout/AppHeader.vue:256:129', 'partially_tested', '只确认退出动作已执行，没有明确最终登录页结果，不判完整通过'),
]

ledger = deepcopy(base)
rows = {row['row_id']: row for row in ledger['controls']}
assert len(rows) == len(ledger['controls']) == 3080
index = {}
for role, group in groups.items():
    for item in group['observations']:
        index[(role, item['id'])] = {'account_role': role, 'account_id': accounts[role], 'original': deepcopy(item), 'mapped_control_ids': []}
for row in rows.values():
    role = row['account_role']
    row['account_id'] = accounts.get(role)
    row['current_release_observations'] = []
    row['coverage_boundary'] = '尚无本轮该控件的实际观察；不得由API、本地测试或旧版观察推定通过'
    row['current_release_environment'] = {'version': '3.8.7', 'release': RELEASE, 'base_url': 'https://lijiadong.cn'}
    row['project_scope'] = {'owner_a': '项目164/文件1894', 'member_a': '项目164的reviewer/文件1894', 'owner_b': '项目165/文件1895', 'no_permission': '本账号无业务权限', 'administrator': '尚无管理员真实登录'}[role]
    row['execution_prerequisite'] = ('需用户正常管理员生产登录' if role == 'administrator' else '已有本轮QA登录实证；会话后续状态以收尾证据为准，未测控件仍未测')
for role, observation_id, control_id, status, boundary in SPEC:
    row = rows[f'{role}|{control_id}']
    entry = index[(role, observation_id)]
    assert role != 'administrator' and control_id not in entry['mapped_control_ids']
    entry['mapped_control_ids'].append(control_id)
    item = {'source_file': f'生产浏览器{role}观察.json', 'observation_id': observation_id,
            'status': status, 'original': deepcopy(entry['original']), 'verified_scope': boundary}
    row['current_release_observations'].append(item)
    row['current_release_evidence'].append({'file': str((HERE / item['source_file']).relative_to(ROOT)),
        'observation_id': observation_id, 'observer': '/root', 'mapper': '/root/usage_attribution',
        'supplement': str((HERE / '生产浏览器精确动作补充.json').relative_to(ROOT))})
for row in rows.values():
    items = row['current_release_observations']
    if not items:
        continue
    statuses = {item['status'] for item in items}
    row['current_release_status'] = next(status for status in ['failed', 'blocked', 'partially_tested', 'passed', 'observed'] if status in statuses)
    row['current_release_actual_result'] = '；'.join(item['original']['result'] for item in items)
    row['coverage_boundary'] = '；'.join(item['verified_scope'] for item in items)
    row['blocker'] = None

unmapped_reasons = {
    ('owner_a', 'ui02'): '新标签加载与版本/统计范围是页面级证据，没有唯一按钮动作；旧标签不当作新版本验收',
    ('owner_a', 'ui05'): '功能搜索输入使用v-model，当前616清单未包含该输入事件节点；保留无匹配提示场景',
    ('owner_a', 'ui07'): '仅观察项目列表范围，不表示任一行按钮已点击',
    ('owner_a', 'ui13'): '代码文件标签为组件原生tab/v-model操作，未纳入当前616节点',
    ('owner_a', 'ui15'): '直接打开URL及持续404/request_id属于页面场景，没有按钮动作',
    ('owner_b', 'b02'): '项目管理打开来源未注明侧栏或顶部快捷导航；列表隔离结果保留，不猜导航节点',
    ('owner_b', 'b03'): '新标签访问跨账号URL与拒绝是页面场景，没有按钮动作',
}
for key, entry in index.items():
    entry['unmapped_reason'] = unmapped_reasons.get(key)
    assert bool(entry['mapped_control_ids']) != bool(entry['unmapped_reason'])
    entry['supplemental_boundary'] = {
        ('member_a', 'm02'): '项目管理导航来源未唯一定位；只映射明确项目表格按钮显隐',
        ('member_a', 'm04'): '成员标签点击与两行数据保留为页面观察，不由此推定原生tab以外源码节点已交互',
        ('member_a', 'm05'): '代码标签点击、上传入口隐藏和下载可见保留；下载可能指两个下载源码入口，Python文本行不渲染二进制下载，不猜同名控件',
        ('no_permission', 'n02'): '零权限导航与统计属于页面范围；只映射明确唯一的导出统计报告隐藏按钮',
    }.get(key)

for account in ledger['accounts']:
    role = account['role']
    account['id'] = accounts.get(role)
    account['login'] = '主代理在生产3.8.7使用精确登录按钮成功登录；仅描述实测当时状态' if role in accounts else '尚无管理员正常登录，仍blocked'
    account['scope'] = {'owner_a': 'QA-a项目164负责人', 'member_a': 'QA-a项目164普通reviewer', 'owner_b': 'QA-b项目165负责人', 'no_permission': '无角色/无权限', 'administrator': '未实测'}[role]
status_counts = Counter(row['current_release_status'] for row in rows.values())
summary = {**base['summary'], 'current_release_statuses': dict(status_counts),
    'current_release_passed': status_counts['passed'], 'raw_browser_observations': len(index),
    'mapped_unique_raw_observations': sum(bool(entry['mapped_control_ids']) for entry in index.values()),
    'raw_unmapped_or_supplemental': sum(not entry['mapped_control_ids'] for entry in index.values()),
    'row_observation_links': len(SPEC),
    'rows_with_current_observations': sum(bool(row['current_release_observations']) for row in rows.values()),
    'per_role': {role: dict(Counter(row['current_release_status'] for row in rows.values() if row['account_role'] == role)) for role in [*accounts, 'administrator']},
    'note': 'passed只表示已列明静态按钮场景；动态实例/不完整结果为partially_tested，显隐/禁用为observed。36原观察不等于36个唯一按钮，API不计UI通过。'}
ledger['summary'] = summary
ledger['boundary'] = '在完整保留初始化及旧观察的基础上，叠加本轮真实观察；没有管理员代测，没有模型调用、导出下载实测或完整616控件通过的结论'
ledger['current_browser_observation_index'] = list(index.values())
ledger['current_supplemental_observations'] = [entry for entry in index.values() if not entry['mapped_control_ids']]
ledger['current_observation_provenance'] = '主代理实际CUA动作与AX结果转录；本代理仅离线核源和映射，不冒充第二次浏览器实测'
ledger['current_source_sha256'] = {**SOURCES, str(Path(__file__).relative_to(ROOT)): sha(Path(__file__).read_bytes())}
assert sha(BASE.read_bytes()) == SOURCES[str(BASE.relative_to(ROOT))]
assert all(row['previous_release_observations'] == original['previous_release_observations'] and row['historical_observations'] == original['historical_observations'] for row, original in zip(ledger['controls'], base['controls']))
assert sum(status_counts.values()) == 3080 and status_counts['blocked'] == 616
OUTPUT.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')
columns = list(ledger['controls'][0])
buffer = io.StringIO(newline='')
writer = csv.DictWriter(buffer, fieldnames=columns)
writer.writeheader()
for row in ledger['controls']:
    writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
with OUTPUT.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as stream:
    stream.write(buffer.getvalue())
summary.update(ledger_sha256=sha(OUTPUT.read_bytes()), csv_sha256=sha(OUTPUT.with_suffix('.csv').read_bytes()), initialization_sha256=sha(BASE.read_bytes()))
(HERE / '生产3.8.7-全部控件逐角色汇总.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False))
