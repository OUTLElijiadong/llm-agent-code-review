"""离线重算本轮官方原始下载与旧快照，避免把网页样式变化计为数据变化。"""
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
old_path = ROOT / 'docs/完整待办与漏洞知识更新20260907/证据/安全知识/官方原始快照20260907.zip'
new_path = OUT / '安全官方原始快照20260912.zip'
with ZipFile(old_path) as old_zip, ZipFile(new_path) as new_zip:
    old = {Path(n).name: old_zip.read(n) for n in old_zip.namelist() if not n.endswith('/')}
    new = {Path(n).name: new_zip.read(n) for n in new_zip.namelist() if not n.endswith('/')}
    catalog = json.loads((ROOT / 'backend/app/constants/data/security_catalog.json').read_text())
    assert old['cwe.zip'] == new['cwe.zip']
    assert old['php-changelog.html'] == new['php-changelog.html']
    mapping_counts = []
    for cat in catalog['categories']:
        name = next(n for n in new if n.startswith(cat['code'] + '_') and n.endswith('.md'))
        section = re.split('list of mapped cwes', new[name].decode(), flags=re.I)[-1]
        refs = sorted(set('CWE-' + i for i in re.findall(r'CWE-(\d+)', section)), key=lambda s: int(s[4:]))
        assert refs == cat['cwe_refs']
        mapping_counts.append(len(refs))
    cve_files = [n for n in old if re.fullmatch('CVE-.*\\.json', n)]
    changed = [n for n in cve_files if old[n] != new[n]]
    assert changed == ['CVE-2026-48019.json']
    before = json.loads(old[changed[0]])
    after = json.loads(new[changed[0]])
    assert before['containers']['cna'] == after['containers']['cna']
    assert after['cveMetadata']['dateUpdated'] == '2026-09-08T13:01:38.157Z'
    fallback = {}
    for name in ('laravel-signed-url.html', 'php-tls-dos.html'):
        extract = lambda raw: re.search(r'<div class="markdown-body[^>]*>(.*?)</div>', raw.decode(), re.S).group(1)
        assert extract(old[name]) == extract(new[name])
        fallback[name] = hashlib.sha256(extract(new[name]).encode()).hexdigest()
    ghsa = json.loads(new['GHSA-jh5r-qr3c-85q8.json'])
    refs = json.loads(new['verified_advisories.json'])
    assert ghsa['cve_id'] is None and ghsa['published_at'] == '2026-09-10T09:56:39Z'
    assert refs == json.loads((ROOT / 'backend/app/constants/data/verified_advisories.json').read_text())
    assert len(refs['records']) == 41 and len(refs['supplementary_advisories']) == 1
    addition = refs['supplementary_advisories'][0]
    assert addition['source_sha256'] == hashlib.sha256(new['GHSA-jh5r-qr3c-85q8.json']).hexdigest()
    assert addition['vulnerabilities'] == ghsa['vulnerabilities']
    current = next(r for r in refs['records'] if r['id'] == 'CVE-2026-48019')
    assert current['source_sha256'] == hashlib.sha256(new['CVE-2026-48019.json']).hexdigest()
    assert current['additional_provider_data'] == after['containers']['adp']
summary = {
    '状态': '通过', '复核日期': '2026-09-12',
    '官方分类': 'OWASP 2025 Final', '分类映射数量': mapping_counts,
    'CWE版本': '4.20', '有效弱点': 944, 'CWE原始包与九月七日相同': True,
    'PHP变更日志与九月七日相同': True,
    '官方CVE原始记录核对数': len(cve_files), '官方CVE原始记录变化': changed,
    '变化记录CNA内容相同': True,
    '变化范围': 'CVE-2026-48019于9月8日增加CISA ADP，记录更新时间及原始摘要同步更新',
    '维护者公告正文摘要': fallback,
    'CVE参考记录': 41, '尚无CVE编号的补充公告': 1,
    '新增公告': ghsa['ghsa_id'], '新增公告发布日期': ghsa['published_at'],
    '数据范围': 'PHP生态精选参考集，并非全量CVE/NVD数据库，不表示项目命中',
    '原始快照摘要': hashlib.sha256(new_path.read_bytes()).hexdigest(),
}
(OUT / '安全来源复核摘要.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2))
