"""独立读取原始官方快照、XML和CVE记录重算；不执行生成器写入入口。"""
import collections
import hashlib
import html.parser
import importlib.util
import io
import json
import pathlib
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def digest(data):
    return hashlib.sha256(data).hexdigest()


class MappedLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h2":
            self.active = attrs.get("id") == "list-of-mapped-cwes"
        if self.active and tag == "a":
            match = re.fullmatch(r"https://cwe.mitre.org/data/definitions/([0-9]+).html", attrs.get("href", ""))
            if match:
                self.links.append("CWE-" + match.group(1))

    def handle_endtag(self, tag):
        if tag == "article":
            self.active = False


class Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def main():
    archive_path = HERE / "官方原始快照20260907.zip"
    manifest = json.loads((HERE / "来源哈希清单.json").read_text())
    assert digest(archive_path.read_bytes()) == manifest["snapshot_archive_sha256"]
    result = {"生产数据库连接次数": 0, "运行代码写入次数": 0, "检查": {}}
    checks = result["检查"]
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names)) == 55
        assert all(pathlib.PurePosixPath(name).name == name for name in names)
        assert set(names) == {source["file"] for source in manifest["sources"]}
        snapshots = {name: archive.read(name) for name in names}
    sources = {item["file"]: item for item in manifest["sources"]}
    for name, value in snapshots.items():
        assert len(value) == sources[name]["bytes"]
        assert digest(value) == sources[name]["sha256"]
    checks["快照ZIP与来源清单"] = {"状态": "通过", "压缩成员数": 55,
        "官方原始下载文件": sum(item["source_url"].startswith("https://") for item in sources.values()),
        "本地规范化派生文件": [name for name, item in sources.items() if not item["source_url"].startswith("https://")]}
    backend = json.loads((ROOT / "backend/app/constants/data/security_catalog.json").read_text())
    frontend = json.loads((ROOT / "frontend/src/views/security/security-catalog.json").read_text())
    assert frontend == {key: backend[key] for key in ("metadata", "categories")}
    namespace = "{http://cwe.mitre.org/cwe-7}"
    with zipfile.ZipFile(io.BytesIO(snapshots["cwe.zip"])) as cwe_zip:
        assert cwe_zip.testzip() is None
        xml_root = ET.fromstring(cwe_zip.read(cwe_zip.namelist()[0]))
    entries = {}
    for container_name, kind in (("Weaknesses", "Weakness"), ("Categories", "Category"), ("Views", "View")):
        container = xml_root.find(namespace + container_name)
        for node in container.findall(namespace + kind):
            key = "CWE-" + node.attrib["ID"]
            description = node.find(namespace + "Description")
            if description is None:
                description = node.find(namespace + "Summary")
            entries[key] = {"name": node.attrib["Name"], "kind": kind, "status": node.attrib["Status"],
                            "description": " ".join("".join(description.itertext()).split()) if description is not None else ""}
    assert set(entries) == set(backend["cwe_entries"])
    for key, value in entries.items():
        assert value == {field: backend["cwe_entries"][key][field] for field in value}, key
    active_weaknesses = sum(row["kind"] == "Weakness" and row["status"] != "Deprecated" for row in entries.values())
    assert active_weaknesses == backend["metadata"]["cwe_weakness_count"] == 944
    assert xml_root.attrib["Version"] == backend["metadata"]["cwe_version"] == "4.20"
    assert xml_root.attrib["Date"] == backend["metadata"]["cwe_release_date"] == "2026-04-30"
    all_mapped = []
    category_counts = {}
    for category in backend["categories"]:
        code = category["code"]
        parser = MappedLinks()
        parser.feed(snapshots[code + ".html"].decode())
        expected = sorted(set(parser.links), key=lambda key: int(key.split("-")[1]))
        assert expected == category["cwe_refs"], code
        assert len(parser.links) == len(set(parser.links)), code
        assert category["source_sha256"] == digest(snapshots[code + ".html"])
        assert category["source_url"] == sources[code + ".html"]["source_url"]
        category_counts[code] = len(expected)
        all_mapped += expected
    assert len(all_mapped) == len(set(all_mapped)) == 249
    mapped_kinds = collections.Counter(entries[key]["kind"] for key in all_mapped)
    assert mapped_kinds == {"Weakness": 246, "Category": 3}
    mapped_categories = sorted((key for key in all_mapped if entries[key]["kind"] == "Category"),
                               key=lambda key: int(key.split("-")[1]))
    assert mapped_categories == ["CWE-16", "CWE-320", "CWE-1035"]
    checks["XML与HTML独立解析重算"] = {"状态": "通过", "XML条目总数": len(entries), "有效弱点": active_weaknesses,
        "各分类映射数": category_counts, "映射总数": 249, "映射实体类型": dict(mapped_kinds), "类别标识": mapped_categories}
    module_path = ROOT / "backend/scripts/build_security_catalog.py"
    spec = importlib.util.spec_from_file_location("review_build_security_catalog", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="security-catalog-independent-") as temporary:
        source = pathlib.Path(temporary)
        for name, value in snapshots.items():
            (source / name).write_bytes(value)
        rebuilt = module.build(source, "2026-09-07")
    assert rebuilt["categories"] == backend["categories"]
    assert rebuilt["cwe_entries"] == backend["cwe_entries"]
    checks["生成器及前后端一致性"] = "通过；调用只返回字典的build函数，未运行写入main"

    advisories = json.loads(snapshots["verified_advisories.json"])
    assert advisories == json.loads((ROOT / "backend/app/constants/data/verified_advisories.json").read_text())
    records = advisories["records"]
    assert len(records) == len({row["id"] for row in records}) == 41
    assert backend["metadata"]["cve_reference_sha256"] == digest(snapshots["verified_advisories.json"])
    record_ids = {row["id"] for row in records}
    assert "CVE-2021-21381" not in record_ids
    kinds = collections.Counter()
    for record in records:
        kinds[record["source_kind"]] += 1
        if record["source_kind"] == "CVE 官方记录（CNA）":
            raw = snapshots[record["id"] + ".json"]
            assert digest(raw) == record["source_sha256"]
            original = json.loads(raw)
            assert original["cveMetadata"]["cveId"] == record["id"]
            assert original["cveMetadata"]["state"] == "PUBLISHED"
            cna = original["containers"]["cna"]
            descriptions = [entry["value"] for entry in cna["descriptions"] if entry["lang"].startswith("en")]
            assert record["description"] in descriptions, record["id"]
            assert record["affected"] == cna.get("affected", []), record["id"]
            assert record["updated_at"] == original["cveMetadata"].get("dateUpdated")
            assert record["published_at"] == original["cveMetadata"].get("datePublished")
            assert record["source_url"] == sources[record["id"] + ".json"]["source_url"]
        else:
            matches = [name for name, source in sources.items() if source["source_url"] == record["source_url"]]
            assert len(matches) == 1
            raw = snapshots[matches[0]]
            assert digest(raw) == record["source_sha256"]
            assert record["id"] in raw.decode(), record["id"]
    assert kinds == {"CVE 官方记录（CNA）": 38, "组件维护者官方公告（中文摘要）": 3}
    changelog_parser = Text()
    changelog_parser.feed(snapshots["php-changelog.html"].decode())
    changelog = " ".join(" ".join(changelog_parser.parts).split())
    php2026 = set(re.findall(r"CVE-2026-\d+", changelog))
    assert php2026 <= record_ids
    assert len(php2026) == 18
    assert sum(record["id"].startswith("CVE-2026-") for record in records) == 20
    flatpak = json.loads(snapshots["CVE-2021-21381.json"])["containers"]["cna"]
    assert "flatpak" in json.dumps(flatpak).lower()
    for code in ("CVE-2022-29248", "CVE-2022-31042", "CVE-2022-31043"):
        description = next(row["description"] for row in records if row["id"] == code).lower()
        assert "guzzle" in description
        assert "redirect" in description or "cookie" in description
    markdown = (ROOT / "backend/app/ai/audit_knowledge/known_cves.md").read_text()
    headings = re.findall(r"^## 漏洞参考：(CVE-\d+-\d+)$", markdown, re.M)
    assert len(headings) == len(set(headings)) == 41 and set(headings) == record_ids
    assert "41 条" in markdown and "Flatpak" in markdown
    checks["CVE逐条原始记录和Markdown重核"] = {"状态": "通过", "活动参考条数": 41,
        "来源类型": dict(kinds), "2026编号": 20, "PHP日志2026编号全覆盖": len(php2026),
        "旧误归属已移除": "CVE-2021-21381实际属于Flatpak；原始记录保留作纠错证据"}
    checks["维护者公告人工核验"] = {
        "CVE-2026-48041": "Laravel公告对应<13.12.0/<12.61.1及Jun 8 2026，与中文摘要一致",
        "CVE-2026-12184": "PHP公告对应<8.3.32/<8.4.21/<8.5.6及Jul 2 2026，与中文摘要一致",
        "CVE-2026-9672": "PHP日志明确GD Upgrade libgd，摘要没有编造完整受影响范围",
    }
    result["范围"] = "PHP生态41条参考集，不是完整CVE/NVD库；哈希证明快照/制品一致，不证明项目实际受漏洞影响。"
    result["输入摘要"] = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in (
        archive_path, HERE / "来源哈希清单.json", module_path,
        ROOT / "backend/app/constants/data/security_catalog.json", ROOT / "frontend/src/views/security/security-catalog.json",
        ROOT / "backend/app/constants/data/verified_advisories.json", ROOT / "backend/app/ai/audit_knowledge/known_cves.md")}
    (HERE / "独立安全数据复核.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"检查组": len(checks), "状态": "通过"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
