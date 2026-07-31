"""为生产 Agent 写入可执行的职责边界。

边界配置采用 default-deny：允许清单内的工具可直接执行，审批清单内的
工具必须升级审批，其余工具均阻断。脚本只通过现有 ORM 和工具网关写入，
可重复执行且不会重复创建相同的权限或审计记录。
"""

# ruff: noqa: E402
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.contracts import (
    CONTRACTS,
    PROTECTED_AGENT_CODES,
    validate_contract_catalog,
)
from app.core.database import SessionLocal
from app.models.agent_governance import AgentProfile, AgentToolPermission, ToolCallLog
from app.models.audit_log import AuditLog
from app.services import agent_governance_service, policy_engine, tool_gateway

SOURCE_MARKER = "agent_contract_20260731_v2"
BLOCKED_TOOLS = ("shell", "system_config_writer", "cross_scope_reader")


def _spec(scope: str, allowed: Iterable[str], approvals: Iterable[str] = ()) -> dict:
    return {
        "scope": scope,
        "allowed_tools": sorted(set(allowed)),
        "approval_tools": sorted(set(approvals)),
        "blocked_tools": list(BLOCKED_TOOLS),
    }


BOUNDARIES = {
    "ai_prompt": _spec("提示词组装", ("response_composer", "experience_reader"), ("release_publisher",)),
    "alert": _spec("告警识别与通知", ("observability_reader", "incident_notifier"), ("workflow_dispatch",)),
    "approval": _spec("审批状态管理", ("governance_reader", "policy_evaluator"), ("release_publisher",)),
    "code_file_manager": _spec("代码文件登记", ("source_reader", "evidence_verifier"), ("workflow_dispatch",)),
    "code_reviewer": _spec("代码质量审查", ("source_reader", "evidence_verifier")),
    "cost_controller": _spec("成本与预算分析", ("observability_reader", "governance_reader")),
    "dashboard": _spec("运行指标汇总", ("observability_reader", "governance_reader")),
    "data_integrity": _spec("任务与数据一致性校验", ("governance_reader", "evidence_verifier")),
    "evolution": _spec(
        "改进候选生成", ("experience_reader", "evaluation_runner"), ("knowledge_store", "workflow_dispatch")
    ),
    "incident_responder": _spec(
        "事件响应与复盘", ("observability_reader", "evidence_verifier"), ("workflow_dispatch",)
    ),
    "knowledge_distiller": _spec("知识抽取与蒸馏", ("source_reader", "evidence_verifier"), ("knowledge_store",)),
    "language_detector": _spec("代码语言识别", ("source_reader",)),
    "memory_manager": _spec("记忆索引与检索", ("experience_reader", "governance_reader"), ("knowledge_store",)),
    "model_evaluator": _spec("模型与黄金集评测", ("evaluation_runner", "experience_reader"), ("workflow_dispatch",)),
    "monitor": _spec("指标与 SLA 监控", ("observability_reader",)),
    "orchestrator": _spec("跨 Agent 任务编排", ("governance_reader", "observability_reader"), ("workflow_dispatch",)),
    "policy": _spec("风险策略评估", ("policy_evaluator", "governance_reader"), ("release_publisher",)),
    "project_analyzer": _spec("项目结构与依赖分析", ("source_reader", "evidence_verifier")),
    "project_manager": _spec("项目任务协调", ("governance_reader", "observability_reader"), ("workflow_dispatch",)),
    "quality_evaluator": _spec("质量信号评估", ("evidence_verifier", "evaluation_runner")),
    "reflection": _spec("反思与经验沉淀", ("experience_reader", "evaluation_runner"), ("knowledge_store",)),
    "report_verifier": _spec("报告证据校验", ("report_builder", "evidence_verifier")),
    "reporter": _spec("审查报告生成", ("report_builder", "evidence_verifier"), ("release_publisher",)),
    "review_orchestrator": _spec("审查流程编排", ("governance_reader", "observability_reader"), ("workflow_dispatch",)),
    "rule_manager": _spec(
        "审查规则维护", ("governance_reader", "evidence_verifier"), ("knowledge_store", "release_publisher")
    ),
    "scheduler": _spec("定时任务触发", ("governance_reader", "observability_reader"), ("workflow_dispatch",)),
    "security_sentinel": _spec("安全风险审查", ("source_reader", "evidence_verifier"), ("workflow_dispatch",)),
    "test_verifier": _spec(
        "回归验证与证据归档", ("source_reader", "evidence_verifier", "evaluation_runner"), ("workflow_dispatch",)
    ),
}


def _sync_contract_skills(db, profile: AgentProfile) -> None:
    """Persist domain skill metadata without turning it into an LLM tool."""
    from app.models.agent_governance import AgentSkillBinding

    contract = CONTRACTS[profile.code]
    for skill in contract.skills:
        row = (
            db.query(AgentSkillBinding)
            .filter(
                AgentSkillBinding.agent_code == profile.code,
                AgentSkillBinding.skill_code == skill.code,
            )
            .first()
        )
        if row is None:
            row = AgentSkillBinding(agent_code=profile.code, skill_code=skill.code)
            db.add(row)
        row.skill_name = skill.name
        row.version = "2.0.0"
        row.enabled = 1
        row.config_json = json.dumps(
            {
                "purpose": skill.purpose,
                "usage_rule": skill.usage_rule,
                "invocable": False,
                "owner": profile.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _upsert_permission(db, agent_code: str, tool_code: str, permission: str, risk_level: str, note: str) -> None:
    row = (
        db.query(AgentToolPermission)
        .filter(AgentToolPermission.agent_code == agent_code, AgentToolPermission.tool_code == tool_code)
        .order_by(AgentToolPermission.id.asc())
        .first()
    )
    if row is None:
        row = AgentToolPermission(agent_code=agent_code, tool_code=tool_code)
        db.add(row)
    row.permission = permission
    row.risk_level = risk_level
    row.enabled = 1
    row.note = note


def _write_boundary_audit(db, profile: AgentProfile, boundary: dict) -> None:
    marker = f"{SOURCE_MARKER}:{profile.code}"
    exists = db.query(AuditLog).filter(AuditLog.target_id == profile.code, AuditLog.detail.like(f"%{marker}%")).first()
    if exists:
        return
    db.add(
        AuditLog(
            actor_id=None,
            actor_name="system-boundary-seeder",
            action="agent",
            target_type="governance_boundary",
            target_id=profile.code,
            detail=json.dumps(
                {
                    "source": marker,
                    "scope": boundary["scope"],
                    "allowed_tools": boundary["allowed_tools"],
                    "approval_tools": boundary["approval_tools"],
                    "blocked_tools": boundary["blocked_tools"],
                },
                ensure_ascii=False,
            ),
            status="success",
            ip="127.0.0.1",
        )
    )


def _run_runtime_checks(db) -> dict[str, str]:
    """通过真实工具网关产生一组可复核的放行、审批、阻断记录。"""
    marker = f"{SOURCE_MARKER}:runtime-check"
    if db.query(ToolCallLog).filter(ToolCallLog.input_summary == marker).count() >= 3:
        return {"status": "already_present"}
    results = {}
    cases = (
        ("allowed", "governance_reader", "governance.read", "agent:review_orchestrator"),
        ("approval", "workflow_dispatch", "workflow.dispatch", "review:live-acceptance"),
        ("blocked", "shell", "shell.read", "workspace"),
    )
    for label, tool_code, action, resource in cases:
        result = tool_gateway.execute(
            db,
            agent_code="review_orchestrator",
            tool_code=tool_code,
            action=action,
            resource=resource,
            input_summary=marker,
            context={"source": SOURCE_MARKER, "check": label},
        )
        results[label] = result.status
    return results


def main() -> None:
    validate_contract_catalog()
    expected_boundaries = set(CONTRACTS) - set(PROTECTED_AGENT_CODES)
    if set(BOUNDARIES) != expected_boundaries:
        raise RuntimeError(
            "Agent 工具边界清单不匹配 "
            f"missing={sorted(expected_boundaries - set(BOUNDARIES))} "
            f"unexpected={sorted(set(BOUNDARIES) - expected_boundaries)}"
        )
    db = SessionLocal()
    try:
        profiles = agent_governance_service.sync_profiles(db)
        profile_codes = {profile.code for profile in profiles}
        missing = sorted(set(CONTRACTS) - profile_codes)
        unexpected = sorted(profile_codes - set(CONTRACTS))
        if missing or unexpected:
            raise RuntimeError(f"Agent 边界清单不匹配 missing={missing} unexpected={unexpected}")

        permission_count = 0
        managed_profiles = [profile for profile in profiles if profile.code not in PROTECTED_AGENT_CODES]
        for profile in managed_profiles:
            boundary = BOUNDARIES[profile.code]
            config = {}
            if profile.config_json:
                try:
                    parsed = json.loads(profile.config_json)
                    if isinstance(parsed, dict):
                        config = parsed
                except json.JSONDecodeError:
                    config = {}
            config["governance_boundary"] = boundary
            config["governance_boundary_version"] = SOURCE_MARKER
            config["agent_contract"] = CONTRACTS[profile.code].governance_config()
            config["agent_contract_version"] = SOURCE_MARKER
            profile.config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)
            _sync_contract_skills(db, profile)

            allowed = set(boundary["allowed_tools"])
            approvals = set(boundary["approval_tools"])
            blocked = set(boundary["blocked_tools"])
            # 保留旧界面最常用的三个工具行，并使其与新边界一致。
            managed_tools = allowed | approvals | blocked | {"source_reader", "release_publisher"}
            for tool_code in sorted(managed_tools):
                if tool_code in blocked:
                    permission, risk = policy_engine.DENY, policy_engine.CRITICAL
                    note = f"{SOURCE_MARKER}：职责边界明确阻断"
                elif tool_code in approvals:
                    permission, risk = policy_engine.ESCALATE, policy_engine.HIGH
                    note = f"{SOURCE_MARKER}：职责边界要求审批"
                elif tool_code in allowed:
                    permission, risk = policy_engine.ALLOW, policy_engine.LOW
                    note = f"{SOURCE_MARKER}：职责域内允许"
                else:
                    permission, risk = policy_engine.DENY, policy_engine.HIGH
                    note = f"{SOURCE_MARKER}：不在职责域内"
                _upsert_permission(db, profile.code, tool_code, permission, risk, note)
                permission_count += 1
            _write_boundary_audit(db, profile, boundary)
        db.commit()
        checks = _run_runtime_checks(db)
        db.commit()
        print(
            json.dumps(
                {
                    "source": SOURCE_MARKER,
                    "profiles": len(profiles),
                    "protected_unchanged": sorted(PROTECTED_AGENT_CODES),
                    "contracts_applied": len(managed_profiles),
                    "permissions_touched": permission_count,
                    "runtime_checks": checks,
                    "boundary_configured": len(managed_profiles),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
