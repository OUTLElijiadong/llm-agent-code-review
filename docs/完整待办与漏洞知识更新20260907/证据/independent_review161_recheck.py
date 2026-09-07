"""独立重数已采集结构及同版纯解析器；不连接数据库或发送模型请求。"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(name):
    return json.loads((EVIDENCE / name).read_text())


def main():
    initial = read('审查任务161生产只读复现.json')
    structure = read('审查任务161报告结构只读复现.json')
    actual = read('审查任务161真实报告解析核验.json')
    before = next(row for row in initial['rows'] if row['kind'] == 'task')
    assert before['id'] == structure['task_id'] == actual['task_id'] == 161
    assert before['project_id'] == 157  # 审查任务161与另一个项目161不能混淆。
    assert before['total_issues'] == actual['stored_total'] == 16
    assert before['status'] == actual['status'] == 'success'
    assert before['update_time'] == actual['updated']
    assert structure['read_only'] and actual['read_only'] == 1
    assert actual['business_writes'] is False and actual['actual_model_http_sent'] is False

    # 按已采集节点顺序重数父发现及字段，不依赖被审解析器的标题算法。
    groups = []
    for node in structure['outline']:
        assert not node['severity_labels']
        if node['node'] == 'heading':
            assert node['level'] == 3 and node['field_label'] is None
            groups.append([])
        else:
            assert node['node'] == 'bullet' and groups
            groups[-1].append(node['field_label'])
    assert len(groups) == 4
    assert all(fields == ['漏洞位置', 'vulnerable code', 'POC', '修复建议'] for fields in groups)
    assert sum(map(len, groups)) == structure['legacy_bullet_count'] == 16
    summary = actual['summary']
    assert summary['total'] == summary['unclassified'] == 4
    assert summary['severity_counts'] == {'严重': 0, '高': 0, '中': 0, '低': 0}

    helper = ROOT / 'backend/app/services/sandbox_report_summary.py'
    collector = EVIDENCE / 'collect_review161_summary_readonly.py'
    assert digest(helper) == actual['helper_sha256']
    assert digest(collector) == actual['collector_sha256']
    tree = ast.parse(collector.read_text())
    sql = next(ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SQL' for t in node.targets))
    assert hashlib.sha256(sql.encode()).hexdigest() == actual['query_sha256']
    statements = [statement.strip() for statement in sql.split(';') if statement.strip()]
    assert len(statements) == 6
    assert [statement.split()[0] for statement in statements] == ['SET', 'SET', 'SET', 'START', 'SELECT', 'ROLLBACK']
    assert 'TRANSACTION READ ONLY' in statements[2] and 'READ ONLY' in statements[3]
    assert "t.id=161 AND t.review_type='sandbox_test'" in statements[4]
    assert 'r.user_id=t.user_id' in statements[4]

    spec = importlib.util.spec_from_file_location('independently_loaded_report_summary', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    counterexamples = read('审查报告边界独立反例.json')
    repaired = []
    for case in counterexamples['cases']:
        observed = module.summarize_sandbox_report(case['report'])
        assert observed['total'] == case['expected_total'], case['name']
        repaired.append({'name': case['name'], 'before_total': case['actual']['total'],
                         'expected_total': case['expected_total'], 'after': observed, 'passed': True})
    sources = ['审查任务161生产只读复现.json', '审查任务161报告结构只读复现.json',
               '审查任务161真实报告解析核验.json', '审查报告边界独立反例.json',
               'collect_review161_summary_readonly.py']
    result = {
        'passed': True, 'reviewer': 'permission_matrix',
        'independent_structure_counts': {'findings': len(groups), 'field_bullets': sum(map(len, groups)),
                                         'explicit_severity_labels': 0},
        'original_task_state_preserved_in_collected_evidence': True,
        'same_helper_collector_and_readonly_query_fingerprints': True,
        'source_sha256': {name: digest(EVIDENCE / name) for name in sources},
        'helper_sha256': digest(helper), 'reviewer_script_sha256': digest(Path(__file__)),
        'boundary_rechecks': repaired,
        'limits': '报告原文未复制到本地，无法在本地重算原文SHA；独立重数的是已采集结构，并核对生产同版解析器指纹与只读事务。未再次查询生产库。',
    }
    (EVIDENCE / '审查任务161独立数据复核.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'findings': 4, 'field_bullets': 16,
                      'independent_boundary_cases': len(repaired)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
