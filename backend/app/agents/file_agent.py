"""
代码文件管理 Agent — 负责文件的查询
"""
import hashlib
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.source_context import SourceContextError, compact_source_context
from app.services import code_file_service

_DIRECT_SOURCE_CHARS = 12_000
_SOURCE_CHUNK_CHARS = 12_000


class CodeFileManagerAgent(BaseAgent):
    """代码文件管理 Agent"""

    name = "code_file_manager"
    description = "代码管家:帮你查项目里有哪些文件、按名字找代码、看任意文件内容"
    icon = "code_file_manager"
    color = "#E76F51"
    category = "manager"
    skills = ("文件列表查询", "文件元数据", "代码内容定位")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user = None

    def _init_skills(self) -> None:
        """子类 override:挂载 CodeFileManagerSelfImprovementSkill + CodeFileManagerProactiveSkill

        将代码文件管理 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.code_file_manager import (
            CodeFileManagerProactiveSkill,
            CodeFileManagerSelfImprovementSkill,
        )

        self.attach_skill(CodeFileManagerSelfImprovementSkill(self.name))
        self.attach_skill(CodeFileManagerProactiveSkill(self.name))

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def list_files(self, project_id: int, language: str = "",
                   page: int = 1, page_size: int = 50,
                   ctx: Optional[AgentContext] = None, *, keyword: str = "") -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        result = code_file_service.list_files(
            self._db, user=self._user, project_id=project_id,
            language=language, keyword=keyword, page=page, page_size=page_size,
        )
        items = [
            {"id": i.id, "file_name": i.file_name,
             "language": i.language, "size_bytes": i.size_bytes,
             "line_count": i.line_count, "version_no": i.version_no}
            for i in result["items"]
        ]
        return AgentResult(success=True, data={
            "total": result["total"], "items": items,
        })

    def get_file(self, file_id: int,
                 ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        try:
            cf = code_file_service.get_file(self._db, user=self._user, file_id=file_id)
            if getattr(cf, "is_binary", 0) == 1:
                return AgentResult(
                    success=False,
                    error="二进制文件不能作为代码文本读取，请使用文件下载接口",
                    failure_kind="binary_source",
                )
            content = cf.content or ""
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            data = {
                "id": cf.id, "file_name": cf.file_name,
                "language": cf.language, "line_count": cf.line_count,
                "source_sha256": digest, "source_char_count": len(content),
                "coverage_complete": True,
            }
            if len(content) <= _DIRECT_SOURCE_CHARS:
                data.update(content=content, content_mode="full")
                return AgentResult(success=True, data=data)

            chunks = [content[start:start + _SOURCE_CHUNK_CHARS]
                      for start in range(0, len(content), _SOURCE_CHUNK_CHARS)]
            source_chunks = []
            for index, text in enumerate(chunks, start=1):
                chunk_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                source_chunks.append({
                    "source_id": f"file:{cf.id}:{index}/{len(chunks)}:{chunk_digest[:12]}",
                    "sha256": chunk_digest,
                    "text": text,
                })
            compressed = compact_source_context(
                self,
                {
                    "coverage_complete": True,
                    "language": cf.language,
                    "source_file_count": 1,
                    "source_text_file_count": 1,
                    "source_binary_file_count": 0,
                    "source_text_bytes": len(content.encode("utf-8")),
                    "source_chunk_count": len(source_chunks),
                    "source_chunks": source_chunks,
                },
                ctx=ctx,
            )
            data.update(content=None, content_mode="compacted", content_context=compressed)
            return AgentResult(success=True, data=data)
        except SourceContextError as e:
            return AgentResult(success=False, error=str(e), failure_kind="source_coverage_incomplete")
        except Exception as e:
            return AgentResult(success=False, error=str(e))
