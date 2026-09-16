"""纯本地验证比较器及42项实际引用采集器的失败门禁。"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPARE = load('compare_release_readonly')
ATTRIBUTION = load('collect_attribution_readonly')


class ReadonlyContracts(unittest.TestCase):
    def setUp(self):
        self.ledger = json.loads((ROOT / '历史账本生产只读复核.json').read_text())
        self.project = json.loads((ROOT / '项目161发布前字段证据.json').read_text())

    def test_real_baseline_self_comparison(self):
        self.assertEqual(COMPARE.compare_project(self.project, self.project)['field_count'], 8)
        self.assertEqual(len(COMPARE.compare_ledger(self.ledger, self.ledger)), 5)

    def test_mysql_error_despite_zero_shell_exit_is_rejected(self):
        broken = dict(self.ledger, error={'returncode': 1})
        with self.assertRaisesRegex(ValueError, 'ledger_query_error'):
            COMPARE.compare_ledger(self.ledger, broken)

    def test_historical_status_change_is_rejected(self):
        changed = copy.deepcopy(self.ledger)
        changed['historical_task'][0]['task_status'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'historical_field_changed'):
            COMPARE.compare_ledger(self.ledger, changed)

    def test_project_field_change_is_rejected(self):
        changed = copy.deepcopy(self.project)
        changed['field_comparison'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'project_this_release_changed'):
            COMPARE.compare_project(self.project, changed)

    def test_42_reference_coverage_and_ownership_failure(self):
        records = [{'kind': 'transaction', 'schema': 'code_review', 'read_only': 1, 'isolation': 'REPEATABLE-READ'},
                   {'kind': 'snapshot_end', 'read_only': 1}]
        records.extend({'kind': 'reference', 'table': table, 'column': column, 'total_rows': 0, 'populated': 0,
                        'missing_target': 0, 'source_owner_unknown': 0, 'target_owner_unknown': 0, 'owner_mismatch': 0}
                       for table in ATTRIBUTION.TABLES for column in ATTRIBUTION.TARGETS)
        self.assertEqual(len(ATTRIBUTION.summarize(records)['reference_contracts']), 42)
        records[-1]['owner_mismatch'] = 1
        self.assertEqual(ATTRIBUTION.summarize(records)['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'reference_cardinality'):
            ATTRIBUTION.summarize(records[:-1])

    def test_readonly_sql_and_special_owner_contract(self):
        sql = ATTRIBUTION.SQL
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;', sql)
        self.assertTrue(sql.rstrip().endswith('ROLLBACK;'))
        self.assertIn('c.owner_id<>p.user_id', sql)
        self.assertIn('LEFT JOIN agent_team g ON g.id=p.team_id', sql)
        for operation in ('INSERT ', 'UPDATE ', 'DELETE ', 'ALTER ', 'DROP '):
            self.assertNotIn(operation, sql)


if __name__ == '__main__':
    unittest.main(verbosity=2)
