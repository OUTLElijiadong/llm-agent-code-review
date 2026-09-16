"""从已下载的官方快照生成内置安全知识数据；不访问业务数据库。

用法：python scripts/build_security_catalog.py --source-dir /path/to/snapshots
输入包括 OWASP 2025 的 A01.html 至 A10.html、owasp-index 和 cwe.zip。
原始快照和 SHA256 清单随验收证据保留，运行时不联网。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
NAMES_ZH = [
    "失效的访问控制",
    "安全配置错误",
    "软件供应链失败",
    "加密失败",
    "注入",
    "不安全的设计",
    "身份验证失败",
    "软件或数据完整性失败",
    "安全日志与告警失败",
    "异常条件处理不当",
]
NS = {"c": "http://cwe.mitre.org/cwe-7"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, verified_at: str) -> dict:
    with ZipFile(source / "cwe.zip") as archive:
        root = ET.fromstring(archive.read(archive.namelist()[0]))
    entries = {}
    for kind in ("Weakness", "Category", "View"):
        for node in root.findall(f".//c:{kind}", NS):
            description = node.find("c:Description", NS)
            if description is None:
                description = node.find("c:Summary", NS)
            entries[f"CWE-{node.attrib['ID']}"] = {
                "name": node.attrib["Name"],
                "kind": kind,
                "status": node.attrib["Status"],
                "description": " ".join("".join(description.itertext()).split()) if description is not None else "",
                "source_url": f"https://cwe.mitre.org/data/definitions/{node.attrib['ID']}.html",
            }
    index = (source / "owasp-index").read_text()
    paths = sorted(set(re.findall(r'href="(A\d\d_2025-[^"#]+/)"', index)))
    if len(paths) != 10:
        raise ValueError("OWASP 2025 索引必须包含十个分类")
    categories = []
    for position, path in enumerate(paths):
        code = path[:3]
        page = source / f"{code}.html"
        content = page.read_text()
        heading = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.S)
        name = html.unescape(re.sub(r"<[^>]+>", "", heading.group(1))).strip()
        name = re.sub(r"^A\d\d:2025\s+", "", name)
        # 只解析官网明确列出的映射列表，不能把正文举例或参考链接算入。
        section = content.split('id="list-of-mapped-cwes"', 1)[1].split("</article>", 1)[0]
        cwes = sorted(set(re.findall(r"CWE-(\d+)", section)), key=int)
        refs = [f"CWE-{number}" for number in cwes]
        missing = set(refs) - entries.keys()
        if missing:
            raise ValueError(f"官方映射不存在于 CWE 数据中：{sorted(missing)}")
        categories.append(
            {
                "code": code,
                "name_en": name,
                "name_zh": NAMES_ZH[position],
                "owasp": f"{code}:2025-{name}",
                "cwe_refs": refs,
                "weakness_count": sum(entries[cwe]["kind"] == "Weakness" for cwe in refs),
                "source_url": "https://owasp.org/Top10/2025/" + path,
                "source_sha256": digest(page),
            }
        )
    mapped = {cwe for category in categories for cwe in category["cwe_refs"]}
    metadata = {
        "owasp_version": "2025",
        "owasp_release": "Final",
        "owasp_source_url": "https://owasp.org/Top10/2025/",
        "cwe_version": root.attrib["Version"],
        "cwe_release_date": root.attrib["Date"],
        "cwe_source_url": "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip",
        "cwe_source_sha256": digest(source / "cwe.zip"),
        "cwe_weakness_count": sum(e["kind"] == "Weakness" and e["status"] != "Deprecated" for e in entries.values()),
        "mapped_cwe_count": len(mapped),
        "mapped_weakness_count": sum(entries[cwe]["kind"] == "Weakness" for cwe in mapped),
        "verified_at": verified_at,
        "mapping_note": (
            "OWASP 各分类页面合计列出 249 个 CWE 标识；按 MITRE 4.20 类型核对为 246 个 Weakness "
            "和 3 个 Category（16、320、1035）。官网介绍页另称 248，本库按可复核列表计数。"
        ),
        "cve_scope": "经官方记录核验的 PHP 生态参考集；不代表全量 CVE/NVD 数据库，也不代表项目已命中漏洞。",
        "licenses": ["OWASP CC BY 3.0", "MITRE CWE Terms of Use: https://cwe.mitre.org/about/termsofuse.html"],
    }
    if metadata["mapped_cwe_count"] != sum(len(c["cwe_refs"]) for c in categories):
        raise ValueError("存在跨分类重复 CWE，需要人工核对后调整映射策略")
    return {"metadata": metadata, "categories": categories, "cwe_entries": entries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--verified-at", default="2026-09-07")
    args = parser.parse_args()
    catalog = build(args.source_dir, args.verified_at)
    advisory_file = args.source_dir / "verified_advisories.json"
    if advisory_file.exists():
        advisories = json.loads(advisory_file.read_text())
        records = advisories["records"]
        catalog["metadata"]["cve_reference_count"] = len(records)
        advisory_verified_at = advisories.get("verified_at", args.verified_at)
        catalog["metadata"]["cve_verified_at"] = advisory_verified_at
        supplemental = advisories.get("supplementary_advisories", [])
        catalog["metadata"]["supplementary_advisory_count"] = len(supplemental)
        catalog["metadata"]["cve_reference_sha256"] = digest(advisory_file)
        catalog["metadata"]["cve_sources"] = advisories["sources"]
        catalog["metadata"]["cve_scope"] = (
            f"已核验 {len(records)} 条 PHP 生态 CVE 参考记录，另有 {len(supplemental)} 条尚无 CVE 编号的维护者公告；"
            f"参考记录核验于 {advisory_verified_at}。不代表全量 CVE/NVD 数据库，也不代表项目已命中漏洞。"
        )
        target = ROOT / "backend/app/constants/data/verified_advisories.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(advisories, ensure_ascii=False, indent=2) + "\n")
        lines = [
            "# PHP 生态漏洞参考库",
            "",
            (
                f"核验日期：{advisory_verified_at}。共 {len(records)} 条 CVE 参考记录，"
                f"另有 {len(supplemental)} 条尚无 CVE 编号的维护者公告。"
            ),
            "",
            (
                "**判定**：必须核对目标的精确组件版本、运行环境、配置及可达代码路径。"
                "版本命中只能形成候选，不能直接确认可利用。HTTP 非 404、500 或版本号相似均不足以确认漏洞。"
                "此参考集不是全量 NVD 数据库，不执行主动探测。"
            ),
            "",
            "原始结构化记录及逐条来源哈希：`app/constants/data/verified_advisories.json`。",
            "",
        ]
        for record in records:
            lines.extend(
                [
                    f"## 漏洞参考：{record['id']}",
                    f"- **来源标题**：{record['title']}",
                    f"- **组件与受影响范围**：{record['affected_summary']}",
                    f"- **判定依据（官方原文）**：{record['description']}",
                    f"- **官方来源**：{record['source_url']}",
                    (
                        f"- **来源类型**：{record['source_kind']}；"
                        f"记录更新时间：{record.get('updated_at') or '来源未提供'}。"
                    ),
                    "",
                ]
            )
        for record in supplemental:
            lines.extend(
                [
                    f"## 补充维护者公告：{record['id']}",
                    f"- **来源标题**：{record['title']}",
                    "- **CVE 编号**：官方尚未提供；不计入 CVE 参考数量。",
                    f"- **组件与受影响范围**：{record['affected_summary']}",
                    f"- **判定依据**：{record['description']}",
                    f"- **官方来源**：{record['source_url']}",
                    (
                        f"- **来源类型**：{record['source_kind']}；"
                        f"记录更新时间：{record.get('updated_at') or '来源未提供'}。"
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "## 历史资料纠正",
                "",
                (
                    "旧速查将 CVE-2021-21381 归为 Symfony 调试远程代码执行，"
                    "官方记录实际为 Flatpak 的沙箱逃逸，已从 PHP 活动参考集移除。"
                ),
                "Guzzle 的 CVE-2022-29248/31042/31043 为重定向或跨域敏感头/凭据泄露，不能仅据此宣称所有版本有 SSRF。",
                (
                    "未附官方编号或未验证版本范围的 ThinkPHP、Monolog、Twig 旧条目仅能作为一般代码模式线索，"
                    "不能作为已确认组件 CVE。历史审查结果不因本库更新而改写。"
                ),
                "",
            ]
        )
        (ROOT / "backend/app/ai/audit_knowledge/known_cves.md").write_text("\n".join(lines))
    output = ROOT / "backend/app/constants/data/security_catalog.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    frontend = ROOT / "frontend/src/views/security/security-catalog.json"
    frontend.write_text(
        json.dumps({key: catalog[key] for key in ("metadata", "categories")}, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(catalog["metadata"], ensure_ascii=False))


if __name__ == "__main__":
    main()
