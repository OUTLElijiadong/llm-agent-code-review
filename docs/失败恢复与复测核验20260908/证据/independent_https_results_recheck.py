#!/usr/bin/env python3
"""只读重数 HTTPS 原始结果与实际执行计划；不导入执行 runner、不发请求。"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCES = {}
CHECKS = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(name):
    path = HERE / name
    raw = path.read_bytes()
    SOURCES[name] = sha(raw)
    return json.loads(raw)


fixtures = read('生产HTTPS夹具验收原始结果.json')
matrix = read('生产HTTPS完整权限矩阵.json')
first_plan = read('生产首轮不完整发现计划.json')
full_plan = read('生产实际完整权限执行计划.json')
local_full_plan = read('生产HTTPS权限计划-最终.json')
snapshot = read('本轮QA验收结束只读快照.json')
runner = ROOT / 'backend/scripts/verify_permission_acceptance_https.py'
SOURCES[str(runner.relative_to(ROOT))] = sha(runner.read_bytes())

CHECKS['两组结果和内容断言全部通过'] = all(
    result['status'] == 'passed' and all(row['status'] == 'passed' and row['actual'] == row['expected'] for row in result['requests'])
    and all(row['passed'] is True for row in result['assertions'])
    and result['summary'] == {'passed': len(result['requests'])} for result in [fixtures, matrix])
CHECKS['实际请求与断言数量'] = (len(fixtures['requests']) == 92 and len(fixtures['assertions']) == 38
    and len(matrix['requests']) == 550 and len(matrix['assertions']) == 11)
CHECKS['两组请求编号无重复'] = len({row['request_id'] for result in [fixtures, matrix] for row in result['requests']}) == 642
CHECKS['响应摘要与请求编号存在'] = all(
    isinstance(row['response_bytes'], int) and row['response_bytes'] >= 0 and re.fullmatch('[a-f0-9]{64}', row['response_sha256'])
    and bool(row['response_request_id']) for result in [fixtures, matrix] for row in result['requests'])
CHECKS['原始结果未保存响应正文或认证材料'] = all(
    not ({'body', 'response_body', 'password', 'token', 'access_token', 'authorization', 'headers'} & row.keys())
    for result in [fixtures, matrix] for row in result['requests'])
CHECKS['两组执行计划规范摘要准确绑定'] = all(
    sha(json.dumps(plan, sort_keys=True).encode()) == result['plan_sha256']
    for plan, result in [(first_plan, fixtures), (full_plan, matrix)])
CHECKS['完整实际计划与冻结计划完全相同'] = full_plan == local_full_plan
CHECKS['完整计划三百一十三路由四十九源码'] = len(full_plan['routes']) == 313 and len(full_plan['source_sha256']) == 49
for title, plan in [('完整', full_plan), ('首轮', first_plan)]:
    CHECKS[title + '计划源码仍与发布源码相同'] = all(
        not Path(name).is_absolute() and '..' not in Path(name).parts and '.venv' not in Path(name).parts
        and sha((ROOT / 'backend' / name).read_bytes()) == digest for name, digest in plan['source_sha256'].items())
    assert len({(r['method'], r['path']) for r in plan['routes']}) == len(plan['routes'])
    result = matrix if title == '完整' else fixtures
    expected, blocked = [], []
    for account, status in [('anonymous', 401), ('no_permission', 403)]:
        for route in plan['routes']:
            if route[account] == 'ready':
                expected.append((account, route['method'], re.sub(r'\{[^}]+\}', '-1', route['path']), status))
            else:
                blocked.append((account, route['method'], route['path']))
    actual = [(r['account'], r['method'], r['path'], r['actual']) for r in result['requests'] if r['purpose'] == '全路由前置认证/授权拒绝']
    CHECKS[title + '每项实际负向请求与计划完全对应'] = Counter(expected) == Counter(actual)
    CHECKS[title + '未请求计划项完整保留阻塞原因'] = Counter(blocked) == Counter((r['account'], r['method'], r['path']) for r in result['blocked'])

negative = [r for r in matrix['requests'] if r['purpose'] == '全路由前置认证/授权拒绝']
CHECKS['完整矩阵实际二九九个四零一与二三九个四零三'] = Counter((r['account'], r['actual']) for r in negative) == {('anonymous', 401): 299, ('no_permission', 403): 239}
CHECKS['完整矩阵十二项登录及自身读取'] = Counter((r['account'], r['purpose'], r['actual']) for r in matrix['requests'] if r['purpose'] != '全路由前置认证/授权拒绝') == Counter(
    (role, purpose, 200) for role in ['owner_a', 'member_a', 'owner_b', 'no_permission'] for purpose in ['专用账号真实登录', '自身真实角色', '自身真实权限'])
CHECKS['首轮三路由未执行完整矩阵'] = (len(first_plan['routes']) == 3 and len(first_plan['source_sha256']) == 10
    and {r['path'] for r in first_plan['routes']} == {'/healthz', '/readyz', '/metrics'}
    and not any(r['purpose'] == '全路由前置认证/授权拒绝' for r in fixtures['requests']) and len(fixtures['blocked']) == 6)
CHECKS['夹具资源与本轮账号对应'] = (fixtures['resources']['a']['project_id'] == 164 and fixtures['resources']['a']['file_id'] == 1894
    and fixtures['resources']['a']['reviewer_user_id'] == 108 and fixtures['resources']['b']['project_id'] == 165 and fixtures['resources']['b']['file_id'] == 1895)
CHECKS['夹具跨账号与同项目成员拒绝均有实际请求'] = Counter(
    (r['purpose'], r['actual']) for r in fixtures['requests'] if r['purpose'] in ['跨项目真实资源拒绝', '同项目reviewer写入拒绝']) == {('跨项目真实资源拒绝', 404): 26, ('同项目reviewer写入拒绝', 403): 7}
users = snapshot['snapshot']['users']
CHECKS['收尾快照仍启用且未执行停用'] = (snapshot['status'] == 'passed' and snapshot['account_disable_executed'] is False
    and snapshot['http_requests_sent'] == 0 and snapshot['snapshot']['context']['readonly'] == 1
    and [r['id'] for r in users] == [107, 108, 109, 110] and all(r['status'] == 1 for r in users))
CHECKS['本轮无模型与审查任务'] = snapshot['snapshot']['counts'] == {'ai_call_log': 0, 'review_task': 0}

report = {
    '审阅人': 'usage_attribution', '状态': '通过' if all(CHECKS.values()) else '打回',
    '本次复核生产请求数': 0, '检查': CHECKS, '来源文件SHA256': SOURCES,
    '夹具实测': {'请求': 92, '断言': 38, '跨项目真实资源404': 26, '同项目成员写入403': 7},
    '完整矩阵实测': {'请求': 550, '断言': 11, '匿名401': 299, '无权限403': 239, '登录及自身读取200': 12, '计划路由': 313, '绑定源码': 49, '未发请求的计划角色格': 88},
    '两组共计实际请求': 642, '计划摘要算法': 'sha256(json.dumps(plan, sort_keys=True).encode())；不是文件原字节SHA',
    '收尾账号': [{'id': r['id'], 'status': r['status']} for r in users],
    '范围': [
        '首轮3路由计划没有发出任何完整矩阵负向请求，92仅统计夹具验收，不能作为完整矩阵。',
        '完整313路由的两类计划格共626，538有安全前置拒绝并已请求，另88阻塞/公开/依赖未知不发送。未覆盖所有认证机制或全部业务正向流程。',
        '首次计划SHA疑点由审阅者误用文件原字节SHA导致；复读runner后按实际规范算法已核对一致，不是应用故障。',
        '92夹具请求包含已授权QA项目/文件/成员创建及修改，不宣称该批是生产零写入；本次离线复核没有生产请求或变更。',
        '四QA仍启用，停用待明确批准；浏览器退出与账号停用是不同事实。',
        '本报告不把API成功计入UI通过，也不把642与测试用例数相加。任务错误专项42由另一独审报告负责，三个有效批次总量684只作算术汇总。',
    ],
}
(HERE / '生产HTTPS两组结果独立复核.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'状态': report['状态'], '检查数': len(CHECKS), '失败': [k for k, v in CHECKS.items() if not v]}, ensure_ascii=False))
raise SystemExit(0 if all(CHECKS.values()) else 1)
