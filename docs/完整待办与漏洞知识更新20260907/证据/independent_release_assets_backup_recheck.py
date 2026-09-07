"""Independent local recount of captured release asset and backup evidence."""
import hashlib
import json
import re
from pathlib import Path


p = Path(__file__).resolve().parent
filename = '生产前端逐文件与本轮备份只读核验.json'
d = json.loads((p / filename).read_text())
runtime = json.loads((p / '生产发布运行态与048核验.json').read_text())
events = json.loads((p / '发布日志关键事件.json').read_text())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


a = d['frontend']
b = d['backup']
expected = a['expected_sha256']
served = a['served_expected_sha256']
assert len(expected) == len(served) == a['expected_count'] == 269
assert expected == served
assert all(k.startswith('./') and '..' not in Path(k).parts and re.fullmatch('[a-f0-9]{64}', v)
           for k, v in expected.items())
manifest = sha(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode())
assert manifest == a['manifest_sha256'] == runtime['frontend_files']['manifest_sha256']
assert expected['./index.html'] == a['index_sha256'] == runtime['frontend_files']['index_sha256']
assert a['served_total_count'] == runtime['frontend_files']['served_count'] == 3709
assert d['ledger_sha256'] == runtime['ledger_sha256']
r = next(c for c in runtime['containers'] if c['name'] == 'cr_frontend')
assert all(a[k] == r[k] for k in ['container_id', 'image_id', 'started_at'])
assert d['release'] == runtime['expected_release'] == b['metadata']['git_sha']
assert d['version'] == runtime['version'] == '3.8.6'
assert b['bytes'] == 404458323 and b['sha256'] == b['metadata']['sha256']
assert b['metadata']['table_count'] == 89
assert b['metadata']['alembic_revision'] == '047_review_input_snapshot'
assert b['file'] == b['metadata']['file']
assert b['file'].startswith('code_review_' + b['metadata']['created_at_utc'] + '_')
assert b['metadata']['reason'] == 'pre_deploy'
assert sha((b['sha256'] + '  ' + b['file'] + '\n').encode()) == b['checksum_sidecar_sha256']
assert any('tables=89, alembic=047_review_input_snapshot' in e for e in events['selected_events'])
assert d['collector_sha256'] == sha((p / 'collect_release_assets_backup_readonly.py').read_bytes())
assert d['reused_helper_sha256'] == sha((p / 'collect_release_readonly.py').read_bytes())
assert all(d[k] is False for k in ['production_writes', 'sql_body_read', 'backup_rerun', 'migration_rerun'])
sources = [filename, 'collect_release_assets_backup_readonly.py', 'collect_release_readonly.py',
           '生产发布运行态与048核验.json', '发布日志关键事件.json']
report = {
    'reviewer': 'permission_matrix', 'status': 'passed',
    'source_sha256': {n: sha((p / n).read_bytes()) for n in sources},
    'recomputed': {
        'expected_map_count': len(expected), 'served_expected_map_count': len(served),
        'dual_maps_equal': True, 'canonical_manifest_sha256': manifest,
        'manifest_matches_earlier_runtime': True,
        'frontend_container_identity_and_ledger_match': True,
        'served_total_count_from_captured_runtime': a['served_total_count'],
        'backup_bytes_from_remote_stat': b['bytes'], 'backup_sha256': b['sha256'],
        'backup_meta_sha_matches_remote_file_hash': True,
        'checksum_sidecar_sha256_recomputed': True,
        'metadata_revision': '047_review_input_snapshot', 'metadata_table_count': 89,
        'restore_log_revision_and_count_agree': True,
    },
    'scope': '独立完整读取采集器和已采集JSON，重算269双map及规范化manifest、校验备份白名单契约和checksum sidecar SHA；没有重新SSH、下载或解压SQL，没有重新恢复或逐表计数。原始metadata字节不在本地，不能本地重算其原字节SHA。',
    'code_review': '备份压缩字节流仅计算SHA，不解析SQL正文；BACKUP_FILE限定backups路径及文件名，meta字段白名单和格式校验，不输出环境原文；复用已审只读helper核验资源和容器，采集后复核容器与文件stat。无生产变更语句。',
}
(p / '生产前端与备份补采独立复核.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(report['recomputed'], ensure_ascii=False))
