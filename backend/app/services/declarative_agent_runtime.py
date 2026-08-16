"""DB-driven runtime for published declarative review agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext
from app.agents.contracts import CONTRACTS
from app.agents.orchestrator import get_request_orchestrator
from app.ai.multi_agent import ReviewAgentProfile
from app.core.config import settings
from app.models.custom_agent import (
    CustomAgent,
    CustomAgentRelease,
    CustomAgentVersion,
    CustomSkill,
    CustomSkillVersion,
    ReviewTaskAgentRelease,
)
from app.models.user import User
from app.services import agent_studio_service, tool_gateway


@dataclass(frozen=True)
class DeclarativeReviewAgentDefinition:
    """Fully resolved immutable definition used for one review task."""

    code: str
    name: str
    description: str
    release_id: int
    version_id: int
    version_number: int
    system_prompt: str
    review_focus: str
    temperature: float
    max_tokens: int
    skill_context: str

    def to_profile(self) -> ReviewAgentProfile:
        instruction = "仅输出固定 Issue JSON 契约，不得执行或建议越界操作。"
        if self.skill_context:
            instruction += f"\n已审批 Skill 上下文：\n{self.skill_context}"
        return ReviewAgentProfile(
            code=self.code,
            name=self.name,
            focus=self.review_focus,
            issue_types=("安全漏洞", "潜在Bug", "性能问题", "可维护性", "异常处理", "其他"),
            instruction=instruction,
            system_prompt=self.system_prompt,
            is_custom=True,
            release_id=self.release_id,
            version_id=self.version_id,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


class PublishedAgentCatalog:
    """Read-through catalog; DB stays authoritative across all workers."""

    @staticmethod
    def list(db: Session) -> list[dict[str, Any]]:
        return agent_studio_service.list_catalog(db)

    @staticmethod
    def runtime_metadata(db: Session) -> list[dict[str, Any]]:
        items = []
        for row in agent_studio_service.list_catalog(db):
            items.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "description": row["description"],
                    "icon": "custom_review_agent",
                    "color": "#2F7D6D",
                    "category": "custom_review",
                    "skills": [
                        {
                            "name": skill["skill_code"],
                            "description": f"{skill['skill_type']} v{skill['skill_version']}",
                            "type": skill["skill_type"],
                            "invocable": False,
                            "agent_name": row["code"],
                            "version_id": skill["skill_version_id"],
                        }
                        for skill in row["skills"]
                    ],
                    "status": "idle",
                    "model": settings.deepseek_model,
                    "source": "custom",
                    "owner_id": row["owner_id"],
                    "version_id": row["version_id"],
                    "version_number": row["version_number"],
                    "release_id": row["release_id"],
                }
            )
        return items

    @staticmethod
    def invalidate(reason: str, agent_code: str) -> None:
        """Publish a best-effort cross-worker invalidation signal.

        Runtime reads are intentionally uncached, so Redis outages cannot leave
        workers on stale versions. The signal is retained for observers and a
        future bounded cache.
        """
        if not settings.redis_url:
            return
        try:
            import redis

            client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
            client.publish(
                "agent-catalog-invalidate",
                json.dumps({"reason": reason, "agent_code": agent_code}, ensure_ascii=False),
            )
            client.close()
        except Exception as exc:  # noqa: BLE001 - DB remains authoritative
            logger.warning(f"[PublishedAgentCatalog] Redis 失效事件发布失败: {exc}")


class DeclarativeReviewAgentFactory:
    """Resolve task snapshots into executable review profiles."""

    @classmethod
    def snapshot_profiles(
        cls,
        db: Session,
        task_id: int,
        user: Optional[User] = None,
    ) -> tuple[ReviewAgentProfile, ...]:
        snapshots = agent_studio_service.snapshot_active_releases(db, task_id)
        profiles: list[ReviewAgentProfile] = []
        for snapshot in snapshots:
            definition = cls._resolve_snapshot(db, snapshot, user=user)
            if definition is not None:
                profiles.append(definition.to_profile())
        return tuple(profiles)

    @classmethod
    def resolve_published(
        cls,
        db: Session,
        agent_code: str,
        user: Optional[User] = None,
    ) -> Optional[DeclarativeReviewAgentDefinition]:
        asset = (
            db.query(CustomAgent)
            .filter(CustomAgent.code == agent_code, CustomAgent.is_enabled == 1)
            .first()
        )
        if not asset or not asset.current_published_version_id:
            return None
        release = (
            db.query(CustomAgentRelease)
            .filter(
                CustomAgentRelease.agent_id == asset.id,
                CustomAgentRelease.agent_version_id == asset.current_published_version_id,
                CustomAgentRelease.status == "published",
            )
            .order_by(CustomAgentRelease.id.desc())
            .first()
        )
        if not release:
            return None
        snapshot = ReviewTaskAgentRelease(
            task_id=0,
            release_id=release.id,
            agent_version_id=release.agent_version_id,
            package_manifest_json=release.package_manifest_json,
        )
        return cls._resolve_snapshot(db, snapshot, user=user)

    @classmethod
    def resolve_release(
        cls,
        db: Session,
        agent_code: str,
        *,
        release_id: int,
        version_id: int,
        package_checksum: str = "",
        template_checksum: str = "",
        user: Optional[User] = None,
    ) -> Optional[DeclarativeReviewAgentDefinition]:
        """按团队创建时的不可变发布快照解析，不跟随当前发布指针。"""

        asset = (
            db.query(CustomAgent)
            .filter(
                CustomAgent.code == agent_code,
                CustomAgent.is_enabled == 1,
                CustomAgent.status == "published",
            )
            .first()
        )
        release = db.get(CustomAgentRelease, int(release_id))
        version = db.get(CustomAgentVersion, int(version_id))
        if (
            asset is None
            or release is None
            or version is None
            or int(release.agent_id) != int(asset.id)
            or int(release.agent_version_id) != int(version.id)
            or int(version.agent_id) != int(asset.id)
            or release.status != "published"
            or release.disabled_at is not None
            or version.status != "published"
        ):
            return None
        if package_checksum and release.package_checksum != package_checksum:
            return None
        if template_checksum and version.checksum != template_checksum:
            return None
        snapshot = ReviewTaskAgentRelease(
            task_id=0,
            release_id=release.id,
            agent_version_id=version.id,
            package_manifest_json=release.package_manifest_json,
        )
        return cls._resolve_snapshot(db, snapshot, user=user)

    @classmethod
    def _resolve_snapshot(
        cls,
        db: Session,
        snapshot: ReviewTaskAgentRelease,
        user: Optional[User],
    ) -> Optional[DeclarativeReviewAgentDefinition]:
        release = db.get(CustomAgentRelease, snapshot.release_id)
        version = db.get(CustomAgentVersion, snapshot.agent_version_id)
        asset = db.get(CustomAgent, version.agent_id) if version else None
        if not release or not version or not asset:
            logger.warning(f"[DeclarativeAgent] 快照 {snapshot.id} 依赖缺失，跳过")
            return None
        config = agent_studio_service._load(version.model_config_json, {})
        manifest = agent_studio_service._load(snapshot.package_manifest_json, {})
        skill_context = cls._compile_skills(db, asset.code, manifest, user=user)
        return DeclarativeReviewAgentDefinition(
            code=asset.code,
            name=asset.name,
            description=asset.description or "",
            release_id=release.id,
            version_id=version.id,
            version_number=version.version_number,
            system_prompt=version.prompt,
            review_focus=version.review_focus,
            temperature=float(config.get("temperature", 0.2)),
            max_tokens=int(config.get("max_tokens", 4096)),
            skill_context=skill_context,
        )

    @classmethod
    def _compile_skills(
        cls,
        db: Session,
        agent_code: str,
        manifest: dict[str, Any],
        user: Optional[User],
    ) -> str:
        parts: list[str] = []
        for item in sorted(manifest.get("skills", []), key=lambda value: value.get("position", 0)):
            skill_version_id = int(item.get("skill_version_id") or 0)
            compiled = cls._compile_skill_version(
                db,
                agent_code,
                skill_version_id,
                user=user,
                depth=0,
                path=(),
            )
            if compiled:
                parts.append(compiled)
        return "\n".join(part for part in parts if part)[:12000]

    @classmethod
    def _compile_skill_version(
        cls,
        db: Session,
        agent_code: str,
        skill_version_id: int,
        *,
        user: Optional[User],
        depth: int,
        path: tuple[int, ...],
    ) -> str:
        """Resolve one immutable Skill version without executing arbitrary code."""
        if depth > 8 or skill_version_id in path:
            return "[Skill] 运行时依赖深度或循环校验失败，本节点已跳过。"
        skill_version = db.get(CustomSkillVersion, skill_version_id)
        if not skill_version:
            return ""
        skill = db.get(CustomSkill, skill_version.skill_id)
        label = skill.name if skill else f"Skill v{skill_version_id}"
        definition = agent_studio_service._load(skill_version.definition_json, {})
        if skill_version.skill_type == "llm_transform":
            return f"[{label}] {definition.get('prompt', '')}"
        if skill_version.skill_type == "readonly_tool":
            return cls._run_readonly_tool(db, agent_code, label, definition, user=user)
        if skill_version.skill_type == "agent_delegate":
            target_code = str(definition.get("agent_code") or "")
            target = CONTRACTS.get(target_code)
            if target is not None:
                return (
                    f"[{label}] 委派给内置 Agent {target.name}（{target_code}）复核："
                    f"{target.mission} 仅采用其职责范围内结论，不得扩展权限。"
                )
            target_asset = (
                db.query(CustomAgent)
                .filter(CustomAgent.code == target_code, CustomAgent.is_enabled == 1)
                .first()
            )
            target_version = (
                db.get(CustomAgentVersion, target_asset.current_published_version_id)
                if target_asset and target_asset.current_published_version_id
                else None
            )
            if target_asset is None or target_version is None:
                return f"[{label}] 委派目标 {target_code} 已不可用，本节点已跳过。"
            return (
                f"[{label}] 委派给已发布 Agent {target_asset.name}（{target_asset.code}，"
                f"v{target_version.version_number}）复核。目标职责：{target_version.review_focus}。"
                f"目标约束：{target_version.prompt[:3000]}"
            )
        if skill_version.skill_type == "sequence_workflow":
            steps = definition.get("steps", [])
            rendered: list[str] = []
            next_path = (*path, skill_version_id)
            for index, step in enumerate(steps, start=1):
                target_id = int(step.get("skill_version_id") or 0) if isinstance(step, dict) else 0
                content = cls._compile_skill_version(
                    db,
                    agent_code,
                    target_id,
                    user=user,
                    depth=depth + 1,
                    path=next_path,
                )
                rendered.append(f"步骤 {index}：{content or '依赖不可用，跳过'}")
            return f"[{label}] 按已审批顺序执行：\n" + "\n".join(rendered)
        return ""

    @staticmethod
    def _run_readonly_tool(
        db: Session,
        agent_code: str,
        label: str,
        definition: dict[str, Any],
        user: Optional[User],
    ) -> str:
        if user is None:
            return f"[{label}] 未提供用户上下文，本次不执行只读工具。"
        tool_code = str(definition.get("tool_code") or "")
        arguments = definition.get("arguments") if isinstance(definition.get("arguments"), dict) else {}
        orchestrator = get_request_orchestrator(db, user=user)
        ctx = AgentContext(user_id=user.id, extra={"source": "declarative_agent", "agent_code": agent_code})

        def handler():
            result = orchestrator.call_tool(tool_code, arguments, ctx)
            if not result.success:
                raise RuntimeError(result.error or "只读工具执行失败")
            return result.data

        gateway = tool_gateway.execute(
            db,
            agent_code=agent_code,
            tool_code=tool_code,
            action=f"readonly.{tool_code}",
            resource="review_context",
            handler=handler,
            input_summary="declarative readonly skill",
            actor=user,
            context={"declarative": True},
        )
        if not gateway.success:
            return f"[{label}] 只读工具未放行：{gateway.error or gateway.status}"
        encoded = json.dumps(gateway.data, ensure_ascii=False, default=str)
        return f"[{label}] 只读工具结果：{encoded[:3000]}"


def publish_catalog_invalidation(reason: str, agent_code: str) -> None:
    PublishedAgentCatalog.invalidate(reason, agent_code)
