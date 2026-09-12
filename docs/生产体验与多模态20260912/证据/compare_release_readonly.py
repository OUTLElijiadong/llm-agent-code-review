"""本轮最终发布取证的脱敏文件比较；纯本地，不连接生产。"""
import argparse
import hashlib
import json
from pathlib import Path

HISTORICAL_KEYS = {'historical_task': 'task_id', 'historical_team_tasks': 'task_id',
                   'historical_event': 'id', 'orphan_tool': 'id', 'unresolved_tool': 'id'}
FIELDS = {'id', 'user_id', 'project_name', 'description', 'language', 'status', 'create_time', 'update_time'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def compare_ledger(before, after):
    for document in (before, after):
        require('error' not in document and document.get('business_writes') is False, 'ledger_query_error')
        require(document.get('schema') == 'historical-ledger-readonly-v1', 'ledger_schema')
        require(len(document.get('transaction', [])) == len(document.get('snapshot_end', [])) == 1,
                'ledger_transaction_cardinality')
        require(document['transaction'][0]['read_only'] == document['snapshot_end'][0]['read_only'] == 1,
                'ledger_not_readonly')
        require(document['transaction'][0]['isolation'] == 'REPEATABLE-READ', 'ledger_isolation')
    require(before['query_sha256'] == after['query_sha256'], 'ledger_query_changed')
    result = {}
    for name, key in HISTORICAL_KEYS.items():
        old_rows = before[name]
        new_rows = after[name]
        old = {row[key]: row for row in old_rows}
        new = {row[key]: row for row in new_rows}
        require(len(old) == len(old_rows) and len(new) == len(new_rows), 'duplicate_historical_id')
        changed = [row_id for row_id, value in old.items() if new.get(row_id) != value]
        require(not changed, 'historical_field_changed:' + name)
        result[name] = {'baseline_rows': len(old), 'all_baseline_fields_equal': True,
                        'new_observed_ids': sorted(new.keys() - old.keys())}
    target = {row['task_id']: row['team_id'] for row in after['historical_task']}
    require(target.get(11) == 5 and target.get(35) == 12, 'historical_target_missing')
    return result


def compare_project(before, after):
    for document in (before, after):
        require(document.get('status') == 'passed' and document.get('schema_equal') is True,
                'project_collection_failed')
        require(document['current']['target_rows'] == document['current']['readonly'] == 1,
                'project_target_readonly')
        require(document.get('sql_write_executed') is False and document.get('backup_reread') is False,
                'project_scope_violation')
        require(len(document['field_comparison']) == 8, 'project_field_count')
        require({r['field'] for r in document['field_comparison']} == FIELDS, 'project_field_set')
    old = {r['field']: r['sha256'] for r in before['field_comparison']}
    new = {r['field']: r['sha256'] for r in after['field_comparison']}
    require(old == new and before['row_sha256'] == after['row_sha256'], 'project_this_release_changed')
    require(before['baseline_file_sha256'] == after['baseline_file_sha256'], 'project_old_baseline_changed')
    return {'field_count': 8, 'all_fields_equal_this_release': True, 'row_sha256': after['row_sha256']}


def compare_release(start, end, expected):
    for report in (start, end):
        require(report.get('status') == 'passed' and report.get('expected_release') == expected,
                'release_not_passed')
        require(report.get('business_writes') is False and len(report.get('foreign_keys', [])) == 42,
                'release_fk_scope')
    for key in ('containers', 'foreign_keys', 'alembic_revision', 'running_source_sha256',
                'ledger_sha256', 'frontend_files'):
        require(start[key] == end[key], 'release_changed_during_collection:' + key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-dir', type=Path, required=True)
    parser.add_argument('--after-dir', type=Path, required=True)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    paths = {
        'ledger_before': args.before_dir / '历史账本生产只读复核.json',
        'ledger_after': args.after_dir / '历史账本发布后.json',
        'project_before': args.before_dir / '项目161发布前字段证据.json',
        'project_after': args.after_dir / '项目161发布后.json',
        'release_start': args.after_dir / '发布身份开始.json',
        'release_end': args.after_dir / '发布身份结束.json',
        'attribution': args.after_dir / '42项实际归因引用.json',
    }
    report = {'status': 'failed', 'scope': '纯本地比较脱敏采集文件；多模态资产独立采集',
              'expected_release': args.expected_release}
    try:
        documents = {key: json.loads(path.read_text()) for key, path in paths.items()}
        report['input_sha256'] = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
        compare_release(documents['release_start'], documents['release_end'], args.expected_release)
        attribution = documents['attribution']
        require(attribution.get('status') == 'passed' and attribution.get('expected_release') == args.expected_release,
                'attribution_failed')
        require(attribution.get('business_writes') is False and len(attribution.get('reference_contracts', [])) == 42,
                'attribution_contract_count')
        report['historical_comparison'] = compare_ledger(documents['ledger_before'], documents['ledger_after'])
        report['project_comparison'] = compare_project(documents['project_before'], documents['project_after'])
        require(documents['project_after']['release'] == args.expected_release, 'project_release_mismatch')
        report.update(status='passed', actual_attribution_contracts=42, release_stable_during_collection=True,
                      attribution_before_comparison='未有本轮发布前实际42项引用采集；不虚构前后相等')
    except Exception as error:
        report['failure_type'] = type(error).__name__
        report['failure_code'] = str(error) if isinstance(error, ValueError) else 'input_or_contract_error'
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': report['status']}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
