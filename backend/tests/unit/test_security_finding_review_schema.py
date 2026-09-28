"""安全扫描 API 必须保留对抗复核状态，供报告区分证伪候选。"""

from app.schemas.security import SecurityFindingOut, SecurityScanOut


def test_security_finding_schema_preserves_refutation_status_and_reason():
    result = SecurityScanOut(findings=[SecurityFindingOut(
        title="密码比较",
        severity="严重",
        verification="refuted",
        verification_reason="实际比较的是哈希值。",
    )])

    serialized = result.model_dump()["findings"][0]
    assert serialized["verification"] == "refuted"
    assert serialized["verification_reason"] == "实际比较的是哈希值。"


def test_security_finding_schema_keeps_legacy_missing_status_unknown():
    finding = SecurityFindingOut(title="旧版候选", severity="中")

    serialized = finding.model_dump()
    assert serialized["verification"] is None
    assert serialized["verification_reason"] == ""
