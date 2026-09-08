"""Independent source and XML recount; no network, credentials, or database access."""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


authored = json.loads((BASE / '登录限流实现指纹.json').read_text())
rows = [
    {'path': row['路径'], 'expected': row['SHA256'], 'actual': sha(ROOT / row['路径'])}
    for row in authored['源码']
]
assert all(row['expected'] == row['actual'] for row in rows)
results = []
for name in ['登录安全独立复跑.xml', '登录安全独立复跑Docker准入.xml', '登录UI与HTTP独立复跑.xml']:
    cases = ET.parse(BASE / name).getroot().findall('.//testcase')
    results.append({
        'file': name, 'sha256': sha(BASE / name), 'total': len(cases),
        'failed': sum(row.find('failure') is not None for row in cases),
        'errors': sum(row.find('error') is not None for row in cases),
        'skipped': sum(row.find('skipped') is not None for row in cases),
    })
assert results[1]['total'] == 39 and results[2]['total'] == 32
assert all(row['failed'] == row['errors'] == row['skipped'] == 0 for row in results[1:])
out = {
    'reviewer': '/root/permission_matrix', 'sources_match': True, 'source_checks': rows,
    'independent_runs': results,
    'result': '本轮限流修复范围独立复核通过',
    'initial_environment_failure': '首次默认沙箱无Docker socket权限导致5个setup error，其余34通过；本地准入后39项无跳过通过。',
    'scope': '真实Lua为本地network none容器、随机key并清理；无生产认证或Redis写入。',
    'boundary': '不据此推断用户所述生产现象唯一由Redis故障造成。',
    'collector_sha256': sha(Path(__file__)),
}
(BASE / '登录限流独立复核.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'sources': len(rows), 'backend': 39, 'frontend': 32, 'source_match': True}))
