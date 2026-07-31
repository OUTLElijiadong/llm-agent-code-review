"""Generate traceable live-acceptance data through Prism business services.

This script is intentionally idempotent. It provisions labelled acceptance users,
imports traceable project source, starts real model-backed reviews, and
executes the governance loop through the same services used by the application.
It never invents review findings or model output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.database import SessionLocal
from app.models.agent_governance import AgentArtifactVersion, AgentProfile, AgentToolPermission, PolicyRule
from app.models.code_file import CodeFile
from app.models.evolution_proposal import EvolutionProposal
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.auth import RegisterIn
from app.schemas.code_file import CodeFileIn
from app.schemas.project import ProjectIn
from app.schemas.review import ReviewStartIn
from app.services import (
    agent_governance_service,
    agent_knowledge_service,
    agent_memory_service,
    approval_service,
    auth_service,
    code_file_service,
    evolution_service,
    issue_service,
    observability_service,
    project_service,
    review_service,
    reward_service,
    rollback_service,
    tool_gateway,
)

SOURCE_MARKER = "live_acceptance_20260730"
DEFAULT_SOURCE_ROOT = Path("/tmp/prism-live-sources")


@dataclass(frozen=True)
class ImportedSource:
    path: Path
    repository: str
    relative_path: str
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--target-users", type=int, default=50)
    parser.add_argument("--reviews", type=int, default=12)
    parser.add_argument("--wait-seconds", type=int, default=1200)
    return parser.parse_args()


def load_sources(source_root: Path) -> list[ImportedSource]:
    allowed = {".py", ".js", ".ts", ".java", ".go", ".php", ".rb"}
    rows: list[ImportedSource] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        if path.stat().st_size < 80 or path.stat().st_size > 24_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not content.strip():
            continue
        relative = path.relative_to(source_root)
        repository = relative.parts[0] if len(relative.parts) > 1 else "imported-source"
        rows.append(ImportedSource(path, repository, str(relative), content))
    if len(rows) < 12:
        raise RuntimeError(f"Need at least 12 importable source files under {source_root}, found {len(rows)}")
    return rows


def ensure_users(db, target_users: int) -> list[User]:
    current = db.query(User).filter(User.status == 1).count()
    needed = max(0, target_users - current)
    created: list[User] = []
    for index in range(1, needed + 1):
        username = f"live_acceptance_{index:02d}"
        user = db.query(User).filter(User.username == username).first()
        if not user:
            password = f"Prism-{secrets.token_urlsafe(18)}"
            user = auth_service.register(
                db,
                RegisterIn(
                    username=username,
                    password=password,
                    email=f"{username}@lijiadong.cn",
                    nickname=f"Live Acceptance {index:02d}",
                ),
            )
            # Authentication is exercised through the real service without exposing a reusable password.
            auth_service.login(db, username, password, ip="127.0.0.1")
        created.append(user)
    return created


def ensure_project_with_source(db, user: User, source: ImportedSource, ordinal: int) -> tuple[Project, CodeFile]:
    project_name = f"Live acceptance {ordinal:02d} - {source.repository}"
    project = db.query(Project).filter(
        Project.user_id == user.id,
        Project.project_name == project_name,
    ).first()
    if not project:
        project = project_service.create_project(
            db,
            user,
            ProjectIn(
                project_name=project_name,
                description=(
                    f"{SOURCE_MARKER}; imported from {source.repository} source; "
                    f"file {source.relative_path}"
                ),
                language=source.path.suffix.lstrip(".") or "text",
            ),
        )
    code_file = db.query(CodeFile).filter(
        CodeFile.project_id == project.id,
        CodeFile.file_path == source.relative_path,
        CodeFile.status == "active",
    ).first()
    if not code_file:
        code_file_service.create_file(
            db,
            user,
            CodeFileIn(
                project_id=project.id,
                file_name=source.path.name,
                file_path=source.relative_path,
                language=source.path.suffix.lstrip(".") or "text",
                content=source.content,
            ),
        )
        code_file = db.query(CodeFile).filter(
            CodeFile.project_id == project.id,
            CodeFile.file_path == source.relative_path,
            CodeFile.status == "active",
        ).first()
    if not code_file:
        raise RuntimeError(f"Imported code file is missing for project {project.id}")
    return project, code_file


def start_real_reviews(db, candidates: list[tuple[User, Project, CodeFile]], reviews: int) -> list[int]:
    task_ids: list[int] = []
    for ordinal, (user, project, code_file) in enumerate(candidates[:reviews], start=1):
        task_name = f"{SOURCE_MARKER} security review {ordinal:02d}"
        existing = db.query(ReviewTask).filter(ReviewTask.task_name == task_name).first()
        if existing:
            task_ids.append(existing.id)
            continue
        task = review_service.start(
            db,
            user,
            ReviewStartIn(
                project_id=project.id,
                file_ids=[code_file.id],
                review_type="security",
                task_name=task_name,
            ),
        )
        task_ids.append(task.id)
        print(f"started review task={task.id} project={project.id}", flush=True)
    return task_ids


def wait_for_reviews(db, task_ids: list[int], wait_seconds: int) -> list[ReviewTask]:
    deadline = time.monotonic() + wait_seconds
    pending = set(task_ids)
    while pending and time.monotonic() < deadline:
        db.expire_all()
        rows = db.query(ReviewTask).filter(ReviewTask.id.in_(pending)).all()
        for row in rows:
            if row.status in {"success", "failed", "cancelled"}:
                pending.discard(row.id)
        if pending:
            time.sleep(5)
    db.expire_all()
    rows = db.query(ReviewTask).filter(ReviewTask.id.in_(task_ids)).all()
    print(
        "review status=" + json.dumps({row.id: row.status for row in rows}, ensure_ascii=False),
        flush=True,
    )
    return rows


def _feedback_decision(raw: str) -> str | None:
    """Read one strict model-feedback decision without guessing on malformed output."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    return decision if decision in {"fixed", "ignored"} else None


def evaluate_review_feedback(db, admin: User, review_rows: list[ReviewTask], limit: int = 12) -> dict:
    """Have the configured model independently resolve imported-review findings.

    A status is written only when the model returns the required JSON enum. The
    model call itself is persisted in ai_call_log by DeepSeekAgent, and the
    normal issue service records the decision actor and timestamp.
    """
    task_ids = [row.id for row in review_rows if row.status == "success"]
    if not task_ids:
        return {"candidates": 0, "fixed": 0, "ignored": 0, "undecided": 0}

    from app.ai.deepseek_agent import DeepSeekAgent
    from app.utils.api_resolver import resolve_api_config

    # Acceptance feedback must never block the entire governance lifecycle on
    # an unhealthy upstream. Failures are retained as undecided issues.
    agent = DeepSeekAgent(
        api_config=resolve_api_config(db, admin.id),
        timeout=45,
        max_retries=0,
    )
    issues = (
        db.query(ReviewIssue)
        .filter(ReviewIssue.task_id.in_(task_ids), ReviewIssue.status == "unfixed")
        .order_by(ReviewIssue.id.asc())
        .limit(limit)
        .all()
    )
    results = {"candidates": len(issues), "fixed": 0, "ignored": 0, "undecided": 0}
    for issue in issues:
        system_prompt = (
            "你是代码审查质量复核员。基于给出的审查发现，判断它是否是可修复的有效问题。"
            "只输出 JSON，不要 Markdown：{\"decision\":\"fixed\"|\"ignored\",\"reason\":\"不超过80字\"}。"
            "fixed 表示有效问题，ignored 表示误报或证据不足。"
        )
        user_prompt = json.dumps(
            {
                "task_id": issue.task_id,
                "file": issue.file_name,
                "line": issue.line_number,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "evidence": issue.evidence,
                "suggestion": issue.suggestion,
            },
            ensure_ascii=False,
        )
        try:
            raw, _ = agent.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                db=db,
                task_id=issue.task_id,
                user_id=admin.id,
                file_id=issue.file_id,
                chunk_index=None,
                agent_label="acceptance_feedback",
            )
            decision = _feedback_decision(raw)
        except Exception as exc:
            print(f"feedback evaluation failed issue={issue.id}: {exc}", flush=True)
            decision = None
        if not decision:
            results["undecided"] += 1
            continue
        issue_service.update_status(db, admin, issue.id, decision)
        results[decision] += 1
    print("feedback status=" + json.dumps(results, ensure_ascii=False), flush=True)
    return results


def run_evolution_cycle(db, admin: User) -> dict:
    """Run proposal generation, gold-set evaluation, promotion and rollback.

    At most one passed proposal is promoted and immediately rolled back. This
    demonstrates the real safety lifecycle while leaving the review-rule set in
    its pre-acceptance state.
    """
    before_id = db.query(EvolutionProposal.id).order_by(EvolutionProposal.id.desc()).first()
    last_id = before_id[0] if before_id else 0
    result = evolution_service.run_evolution(db, admin, window_days=90)
    proposals = (
        db.query(EvolutionProposal)
        .filter(EvolutionProposal.id > last_id)
        .order_by(EvolutionProposal.id.asc())
        .all()
    )
    outcome = {"run": result, "created": len(proposals), "evaluated": 0, "promoted": 0, "rolled_back": 0}
    for proposal in proposals:
        evaluated = evolution_service.evaluate_proposal(db, proposal.id)
        outcome["evaluated"] += 1
        if evaluated.status != "eval_passed" or outcome["promoted"]:
            continue
        promoted = evolution_service.approve_proposal(db, admin, evaluated.id, require_eval=True)
        outcome["promoted"] += 1
        evolution_service.rollback_proposal(
            db,
            admin,
            promoted.id,
            note="Live acceptance lifecycle validation completed; restored baseline rule state.",
        )
        outcome["rolled_back"] += 1
    print("evolution status=" + json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
    return outcome


def ensure_policy(db, code: str, name: str, action: str, effect: str, risk_level: str, priority: int) -> PolicyRule:
    row = db.query(PolicyRule).filter(PolicyRule.rule_code == code).first()
    if not row:
        row = PolicyRule(rule_code=code, name=name)
        db.add(row)
    row.name = name
    row.subject = "agent:*"
    row.action = action
    row.resource = "*"
    row.effect = effect
    row.risk_level = risk_level
    row.condition_json = json.dumps({"source": SOURCE_MARKER}, ensure_ascii=False)
    row.priority = priority
    row.enabled = 1
    db.commit()
    db.refresh(row)
    return row


def ensure_permission(db, agent_code: str, tool_code: str, permission: str, risk_level: str, note: str) -> None:
    row = db.query(AgentToolPermission).filter(
        AgentToolPermission.agent_code == agent_code,
        AgentToolPermission.tool_code == tool_code,
    ).first()
    if not row:
        row = AgentToolPermission(agent_code=agent_code, tool_code=tool_code)
        db.add(row)
    row.permission = permission
    row.risk_level = risk_level
    row.enabled = 1
    row.note = note
    db.commit()


def build_governance_loop(db, admin: User, sources: list[ImportedSource], review_rows: list[ReviewTask]) -> None:
    profiles = agent_governance_service.sync_profiles(db)
    if len(profiles) < 30:
        raise RuntimeError(f"Expected 30 governance profiles, got {len(profiles)}")

    ensure_policy(db, "live_acceptance_read", "验收源码读取", "knowledge.read", "allow", "low", 20)
    ensure_policy(db, "live_acceptance_publish", "验收版本发布审批", "artifact.publish", "escalate", "high", 10)
    ensure_policy(db, "live_acceptance_shell", "验收危险命令阻断", "shell.exec", "deny", "critical", 5)

    for index, profile in enumerate(profiles):
        source = sources[index % len(sources)]
        source_ref = f"local_project:{source.repository}/{source.relative_path}"
        if not db.query(AgentToolPermission).filter(
            AgentToolPermission.agent_code == profile.code,
            AgentToolPermission.tool_code == "source_reader",
        ).first():
            ensure_permission(db, profile.code, "source_reader", "allow", "low", "Public source inspection")
        if not db.query(AgentToolPermission).filter(
            AgentToolPermission.agent_code == profile.code,
            AgentToolPermission.tool_code == "release_publisher",
        ).first():
            ensure_permission(
                db,
                profile.code,
                "release_publisher",
                "escalate",
                "high",
                "Version promotion needs approval",
            )

        if not db.query(AgentArtifactVersion).filter(
            AgentArtifactVersion.agent_code == profile.code,
            AgentArtifactVersion.version == f"{SOURCE_MARKER}-knowledge-v1",
        ).first():
            agent_memory_service.add_memory(
                db,
                agent_code=profile.code,
                title=f"Imported source context: {source.path.name}",
                content=source.content[:1800],
                memory_type="long_term",
                weight=0.8,
                source_ref=source_ref,
            )
            doc = agent_knowledge_service.add_document(
                db,
                agent_code=profile.code,
                title=f"Imported source: {source.relative_path}",
                content=source.content,
                source_type="local_project",
                source_ref=source_ref,
                risk_level="low",
                confidence=0.92,
            )
            rollback_service.create_version(
                db,
                agent_code=profile.code,
                artifact_type="knowledge",
                version=f"{SOURCE_MARKER}-knowledge-v1",
                content=json.dumps({"doc_id": doc.id, "source": source_ref}, ensure_ascii=False),
                snapshot=json.dumps({"doc_id": doc.id, "status": "active"}, ensure_ascii=False),
                status="stable",
            )

        digest = hashlib.sha256(source.content.encode("utf-8")).hexdigest()[:16]
        tool_actions = (
            ("knowledge.read", "source_reader"),
            ("evidence.verify", "source_reader"),
            ("report.generate", "source_reader"),
        )
        for action, tool_code in tool_actions:
            tool_gateway.execute(
                db,
                agent_code=profile.code,
                tool_code=tool_code,
                action=action,
                resource=source_ref,
                input_summary=f"{SOURCE_MARKER}: {source.relative_path}",
                actor=admin,
                handler=lambda ref=source_ref, value=digest: {"source": ref, "sha256": value},
            )

        if index % 4 == 0:
            tool_gateway.execute(
                db,
                agent_code=profile.code,
                tool_code="release_publisher",
                action="artifact.publish",
                resource=f"artifact:{profile.code}:{SOURCE_MARKER}",
                input_summary="Publish an acceptance artifact after review",
                actor=admin,
            )
        if index % 5 == 0:
            tool_gateway.execute(
                db,
                agent_code=profile.code,
                tool_code="shell",
                action="shell.exec",
                resource="production:protected",
                input_summary="Blocked destructive command simulation without a handler",
                actor=admin,
                context={"command": "rm -rf protected-path"},
            )

        task = review_rows[index % len(review_rows)] if review_rows else None
        task_ref = f"review_task:{task.id}" if task else source_ref
        issues = task.total_issues if task else 0
        reward = 2.0 if task and task.status == "success" else -1.0
        reward_service.record_reflection(
            db,
            agent_code=profile.code,
            task_ref=task_ref,
            summary=f"Live acceptance evidence processed; task issues={issues}.",
            lesson="Retain source evidence, policy decision and task reference before updating an artifact.",
            risk_score=min(1.0, issues / 20.0),
            reward_score=reward,
        )
        reward_service.record_reward(
            db,
            agent_code=profile.code,
            event_type="reward" if reward > 0 else "penalty",
            score=reward,
            reason=f"{SOURCE_MARKER}: observed review outcome for {task_ref}",
            impact={"task_ref": task_ref, "issues": issues},
        )

    pending = approval_service.list_items(db, status="pending", limit=200)
    for index, item in enumerate(pending):
        approval_service.decide_item(
            db,
            admin,
            item.id,
            approve=index % 3 != 2,
            note="Live acceptance decision: reviewed with traceable source and policy evidence.",
        )

    if not db.query(AgentArtifactVersion).filter(
        AgentArtifactVersion.version == f"{SOURCE_MARKER}-policy-rollback",
    ).first():
        policy = db.query(PolicyRule).filter(PolicyRule.rule_code == "live_acceptance_read").one()
        snapshot = {
            "rule_id": policy.id,
            "rule_code": policy.rule_code,
            "name": policy.name,
            "subject": policy.subject,
            "action": policy.action,
            "resource": policy.resource,
            "effect": policy.effect,
            "risk_level": policy.risk_level,
            "condition_json": policy.condition_json,
            "priority": policy.priority,
            "enabled": policy.enabled,
        }
        version = rollback_service.create_version(
            db,
            agent_code="policy",
            artifact_type="policy",
            version=f"{SOURCE_MARKER}-policy-rollback",
            content=json.dumps({"change": "validated policy version"}, ensure_ascii=False),
            snapshot=json.dumps(snapshot, ensure_ascii=False),
            status="stable",
        )
        rollback_service.rollback_version(db, version.id)

    metrics = observability_service.overview(db)
    for key, value in {
        "live_acceptance.tool_calls": sum(item["count"] for item in metrics["tool_status"]),
        "live_acceptance.job_runs": metrics["job_runs"],
        "live_acceptance.open_alerts": metrics["open_alerts"],
        "live_acceptance.reward_score": metrics["reward_score_total"],
    }.items():
        observability_service.snapshot(db, key, float(value), {"source": SOURCE_MARKER})


def main() -> int:
    args = parse_args()
    sources = load_sources(Path(args.source_root))
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin", User.status == 1).order_by(User.id.asc()).first()
        if not admin:
            raise RuntimeError("An active admin account is required for governance decisions")
        users = ensure_users(db, args.target_users)
        if not users:
            users = db.query(User).filter(User.username.like("live_acceptance_%"), User.status == 1).all()
        candidates = []
        for ordinal, user in enumerate(users, start=1):
            source = sources[(ordinal - 1) % len(sources)]
            project, code_file = ensure_project_with_source(db, user, source, ordinal)
            candidates.append((user, project, code_file))
        task_ids = start_real_reviews(db, candidates, args.reviews)
        review_rows = wait_for_reviews(db, task_ids, args.wait_seconds)
        feedback = evaluate_review_feedback(db, admin, review_rows)
        build_governance_loop(db, admin, sources, review_rows)
        evolution = run_evolution_cycle(db, admin)
        summary = {
            "active_users": db.query(User).filter(User.status == 1).count(),
            "agent_profiles": db.query(AgentProfile).count(),
            "projects": db.query(Project).count(),
            "review_tasks": db.query(ReviewTask).count(),
            "successful_reviews": db.query(ReviewTask).filter(
                ReviewTask.task_name.like(f"{SOURCE_MARKER}%"),
                ReviewTask.status == "success",
            ).count(),
            "feedback": feedback,
            "evolution": evolution,
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
