#!/usr/bin/env python3
"""离线复核发布前后证据；不连接生产、不执行采集器或模型调用。"""
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OLD = ROOT / 'docs/完整待办与漏洞知识更新20260907/证据'
RELEASE = '4035d1902342ab88357b5bb6071545d1a0572dd1'
SOURCES = {}
CHECKS = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def raw(path):
    value = path.read_bytes()
    SOURCES[str(path.relative_to(ROOT))] = sha(value)
    return value


def read(name, folder=HERE):
    return json.loads(raw(folder / name))


def canonical(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def constant(path, name):
    tree = ast.parse(raw(path).decode())
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def assembled_sql(path):
    """仅重建常量、f-string 与有限元组循环；禁止调用和导入采集器。"""
    tree = ast.parse(raw(path).decode())
    body = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AugAssign, ast.For))]
    assert not any(isinstance(node, ast.Call) for stmt in body for node in ast.walk(stmt))
    namespace = {'__builtins__': {}}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['SQL']


before = read('发布前生产版本与外键核验.json')
after = read('发布后生产版本与外键核验.json')
history_before = read('发布前历史账本只读.json')
history_after = read('发布后历史账本只读.json')
project_before = read('发布前项目161八字段核验.json')
project_after = read('发布后项目161八字段核验.json')
review_before = read('发布前审查任务161报告核验.json')
review_after = read('发布后审查任务161报告核验.json')
assets = read('发布后前端资源与备份核验.json')
env = read('生产默认发布环境校准.json')
ops = read('发布后生产运维门禁.json')
deploy_log = raw(HERE / '生产正式发布.log').decode()

CHECKS['运行版本与迁移'] = (after['status'] == 'passed' and after['expected_release'] == RELEASE
    and after['version'] == '3.8.7' and after['alembic_revision'] == before['alembic_revision'] == '048_ai_usage_attribution')
CHECKS['运行证据只读事务'] = after['business_writes'] is False and after['readonly_transaction']['read_only'] == 1
tables = {'agent_response_run', 'agent_team', 'review_task', 'ai_call_log',
          'agent_mesh_message', 'pentest_engagement', 'sandbox_environment'}
targets = {'root_agent_run_id': 'agent_response_run', 'agent_run_id': 'agent_response_run',
           'tool_execution_id': 'agent_tool_execution', 'agent_team_id': 'agent_team',
           'agent_team_task_id': 'agent_team_task', 'agent_execution_event_id': 'agent_team_event'}
fks = after['foreign_keys']
CHECKS['七表精确四十二外键'] = len(fks) == 42 and {(r['table'], r['column']) for r in fks} == {(t, c) for t in tables for c in targets}
CHECKS['外键完整契约与前版相同'] = fks == before['foreign_keys'] and all(
    r['target_schema'] == 'code_review' and r['target'] == targets[r['column']] and r['target_column'] == 'id'
    and r['nullable'] is True and r['delete_rule'] == 'SET NULL' and r['source_type'] == 'bigint'
    and r['column_count'] == 1 for r in fks)
CHECKS['五份运行源码与当前源相同'] = after['running_source_sha256'] == before['running_source_sha256'] and all(
    digest == sha(raw(ROOT / 'backend' / name)) for name, digest in after['running_source_sha256'].items())
CHECKS['当前应用与发布提交相同'] = subprocess.run(
    ['git', 'diff', '--quiet', RELEASE, '--', 'backend', 'frontend', 'VERSION', 'deploy'], cwd=ROOT).returncode == 0

stable = ['historical_task', 'historical_team_tasks', 'historical_event', 'orphan_tool', 'unresolved_tool',
          'relationship', 'tool_integrity', 'owner_mismatch', 'task_member_team_mismatch']
for name in stable:
    CHECKS['历史完整行保持_' + name] = history_before[name] == history_after[name]
CHECKS['两项历史任务状态保持'] = [(r['task_id'], r['team_id'], r['task_status'], r['team_status']) for r in history_after['historical_task']] == [(11, 5, 'queued', 'failed'), (35, 12, 'waiting_dependency', 'failed')]
CHECKS['七孤儿与三执行旧状态保持'] = [r['id'] for r in history_after['orphan_tool']] == [125, 126, 127, 128, 129, 2580, 2581] and [(r['id'], r['status'], r['run_status']) for r in history_after['unresolved_tool']] == [(472, 'executing', 'failed'), (634, 'executing', 'failed'), (2286, 'executing', 'failed')]
history_sql = assembled_sql(OLD / 'collect_historical_ledger.py')
CHECKS['历史查询哈希及只读事务'] = (sha(history_sql.encode()) == history_before['query_sha256'] == history_after['query_sha256']
    and 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;' in history_sql and history_sql.rstrip().endswith('ROLLBACK;')
    and history_after['business_writes'] is False and history_after['transaction'][0]['read_only'] == 1 and history_after['snapshot_end'][0]['read_only'] == 1)
counts = lambda data: {(r['table'], r['status']): r['count'] for r in data['status_counts']}
a, b = counts(history_before), counts(history_after)
deltas = [{'table': key[0], 'status': key[1], 'before': a.get(key, 0), 'after': b.get(key, 0),
           'delta': b.get(key, 0) - a.get(key, 0)} for key in sorted(a.keys() | b.keys()) if a.get(key, 0) != b.get(key, 0)]
CHECKS['仅全局运维计数变化'] = all(row['table'] == 'ops_execution' for row in deltas)
CHECKS['工具账本总数相符'] = sum(v for (table, _), v in b.items() if table == 'agent_tool_execution') == history_after['tool_integrity'][0]['total'] == 2608

fields = ['id', 'user_id', 'project_name', 'description', 'language', 'status', 'create_time', 'update_time']
CHECKS['项目八字段与行哈希保持'] = ([r['field'] for r in project_after['field_comparison']] == fields
    and project_after['field_comparison'] == project_before['field_comparison']
    and project_after['row_sha256'] == project_before['row_sha256']
    and project_after['baseline_file_sha256'] == project_before['baseline_file_sha256'] == sha(raw(OLD / '项目161完整字段比较.json')))
CHECKS['项目结构只读与单个目标'] = (project_after['release'] == RELEASE and project_after['schema_equal'] is True
    and project_after['current']['readonly'] == 1 and project_after['current']['target_rows'] == 1
    and project_after['sql_write_executed'] is False and project_after['backup_reread'] is False)
CHECKS['报告所有非采集时间字段保持'] = {k: v for k, v in review_after.items() if k not in ['utc', 'collected_at_utc']} == {k: v for k, v in review_before.items() if k not in ['utc', 'collected_at_utc']}
review_sql = constant(OLD / 'collect_review161_summary_readonly.py', 'SQL')
CHECKS['报告解析器和查询来源绑定'] = (review_after['helper_sha256'] == sha(raw(ROOT / 'backend/app/services/sandbox_report_summary.py'))
    and review_after['collector_sha256'] == sha(raw(OLD / 'collect_review161_summary_readonly.py'))
    and review_after['query_sha256'] == sha(review_sql.encode()))
CHECKS['报告旧十六与四未分级语义'] = (review_after['stored_total'] == 16 and review_after['status'] == 'success'
    and review_after['updated'] == '2026-08-19 02:23:00.000000'
    and review_after['summary']['total'] == review_after['summary']['unclassified'] == 4
    and sum(review_after['summary']['severity_counts'].values()) == 0
    and review_after['read_only'] == 1 and review_after['business_writes'] is False and review_after['actual_model_http_sent'] is False)

front = assets['frontend']
expected, served = front['expected_sha256'], front['served_expected_sha256']
manifest = sha(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode())
CHECKS['前端双清单逐路径相等且确为二百七十'] = len(expected) == len(served) == front['expected_count'] == after['frontend_files']['expected_count'] == 270 and expected == served
CHECKS['资源路径与摘要有效'] = all(k.startswith('./') and '..' not in Path(k).parts and re.fullmatch('[a-f0-9]{64}', v) is not None for k, v in expected.items())
CHECKS['资源摘要与索引独立重算'] = (manifest == front['manifest_sha256'] == after['frontend_files']['manifest_sha256']
    and expected['./index.html'] == front['index_sha256'] == after['frontend_files']['index_sha256']
    and front['served_total_count'] == after['frontend_files']['served_count'] == 3725)
fc = next(r for r in after['containers'] if r['name'] == 'cr_frontend')
CHECKS['容器镜像与账本跨证据绑定'] = (all(front[k] == fc[k] for k in ['container_id', 'image_id', 'started_at'])
    and front['image_tag'] == fc['image'] == 'prism-frontend:' + RELEASE
    and assets['ledger_sha256'] == after['ledger_sha256'] and assets['release'] == RELEASE and assets['version'] == '3.8.7'
    and all(r['health'] == 'healthy' and r['release'] == RELEASE and r['version'] == '3.8.7' for r in after['containers']))
CHECKS['资源采集器来源与副作用边界'] = (assets['collector_sha256'] == sha(raw(OLD / 'collect_release_assets_backup_readonly.py'))
    and assets['reused_helper_sha256'] == sha(raw(OLD / 'collect_release_readonly.py'))
    and all(assets[k] is False for k in ['production_writes', 'sql_body_read', 'backup_rerun', 'migration_rerun']))
backup = assets['backup']
meta = backup['metadata']
CHECKS['备份白名单及本次发布绑定'] = (set(meta) == {'created_at_utc', 'reason', 'git_sha', 'alembic_revision', 'table_count', 'sha256', 'file'}
    and meta['git_sha'] == RELEASE and meta['reason'] == 'pre_deploy' and meta['table_count'] == 89
    and meta['alembic_revision'] == after['alembic_revision']
    and backup['file'] == meta['file'] == f"code_review_{meta['created_at_utc']}_{RELEASE[:12]}.sql.gz"
    and backup['directory_scope'] == '/opt/code-review/backups' and backup['release_ledger_backup_file_matches'] is True)
CHECKS['备份大小与远程三方哈希一致'] = backup['bytes'] == 405224409 and backup['sha256'] == meta['sha256'] and backup['actual_file_meta_and_sidecar_sha_equal'] is True
CHECKS['备份校验旁文件哈希独立重算'] = sha((backup['sha256'] + '  ' + backup['file'] + '\n').encode()) == backup['checksum_sidecar_sha256']
CHECKS['日志还原表数与镜像和完成事件'] = ('独立恢复验证通过(container=cr_testdb, tables=89, alembic=048_ai_usage_attribution)' in deploy_log
    and f'发布完成(target=all, sha={RELEASE}, alembic=048_ai_usage_attribution)' in deploy_log
    and all(r['image_id'] in deploy_log for r in after['containers']))
CHECKS['默认环境警告已保留并有后续校准门禁'] = ('ERROR Compose default 发布环境漂移: APP_RELEASE' in deploy_log
    and env['status'] == 'aligned' and env['release'] == RELEASE and env['version'] == '3.8.7'
    and set(env['changed_fields']) == {'APP_RELEASE', 'APP_VERSION', 'BACKEND_RELEASE', 'FRONTEND_RELEASE'}
    and env['unrelated_bytes_preserved'] is True and env['file_mode'] == '0600'
    and ops['status'] == 'ok' and ops['can_continue'] is True and ops['blocking_checks'] == []
    and ops['checks']['release']['ok'] is True and ops['checks']['backup']['file'] == backup['file'])

report = {
    '审阅人': 'usage_attribution', '状态': '通过' if all(CHECKS.values()) else '打回',
    '发布提交': RELEASE, '版本': '3.8.7', '生产请求数': 0, '生产写入': False, '重复测试或迁移': False,
    '检查': CHECKS, '来源指纹': SOURCES,
    '外键逐表重数': dict(sorted(Counter(r['table'] for r in fks).items())),
    '历史指定行': {key: {'count': len(history_after[key]), 'canonical_sha256': canonical(history_after[key])} for key in stable},
    '全局动态计数差异': deltas,
    '前端': {'镜像应有文件': len(expected), '公开目录文件总数': front['served_total_count'], '双清单相等': expected == served,
             '独立重算规范摘要': manifest, '索引摘要': expected['./index.html']},
    '备份': {'file': backup['file'], 'bytes': backup['bytes'], 'sha256': backup['sha256'], 'revision': meta['alembic_revision'], 'table_count': meta['table_count']},
    '项目行摘要': project_after['row_sha256'], '报告原文摘要': review_after['report_md_sha256'], '报告摘要': review_after['summary'],
    '证据边界': [
        '独立离线重算已采集JSON与源码、完整读取正式日志；未再SSH、发HTTP、读取业务原文、执行模型或改写生产。',
        '本轮资源清单为270，早期发布前计划沿用269仅为计划；本报告按实际镜像与served双清单重数。3725全目录仅有采集器摘要，未独立重算该完整目录的逐路径SHA。',
        '备份gzip字节大小与SHA来自已审采集器的远程实际读取；本地只重算校验旁文件规范字节SHA，未重新下载、解压或恢复；metadata原始字节未归档，未声称本地重算其SHA。',
        '日志证明独立恢复89表/048和发布完成事件，没有BACKUP_FILE路径或进程退出码行；备份精确关联来自collector核对发布账本，exit0为主代理工具执行证据。',
        '默认环境校准记录为主代理执行结果；本审未读取.env原文。后续运维门禁确认账本、镜像与默认环境一致。',
        '全局ops计数差不能据此推定调用人或业务原因。指定历史行完整保持，不将其旧异常自动修为终态。',
        '四条报告发现仍未分级，不等于四个已确认漏洞或零风险；本地不重新解析未下载的敏感Markdown，核对报告hash、同版helper与前后完整摘要。',
        '本结论仅覆盖版本、外键、指定历史数据、资源与备份；不代表全部角色按钮已点击验收。',
    ],
}
(HERE / '3.8.7发布后数据与备份独立复核.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'状态': report['状态'], '检查数': len(CHECKS), '失败': [key for key, ok in CHECKS.items() if not ok]}, ensure_ascii=False))
raise SystemExit(0 if all(CHECKS.values()) else 1)
