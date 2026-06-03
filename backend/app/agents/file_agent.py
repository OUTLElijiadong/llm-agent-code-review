"""
代码文件管理 Agent — 负责文件的查询
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.services import code_file_service


class CodeFileManagerAgent(BaseAgent):
    """代码文件管理 Agent"""

    name = "code_file_manager"
    description = "查询项目代码文件列表和详情"
    icon = "code_file_manager"
    color = "#E76F51"
    category = "manager"
    skills = ("文件列表查询", "文件元数据", "代码内容定位")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user = None

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def list_files(self, project_id: int, language: str = "",
                   page: int = 1, page_size: int = 50,
                   ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        result = code_file_service.list_files(
            self._db, user=self._user, project_id=project_id,
            language=language, page=page, page_size=page_size,
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
            return AgentResult(success=True, data={
                "id": cf.id, "file_name": cf.file_name,
                "language": cf.language, "content": cf.content[:3000],
                "line_count": cf.line_count,
            })
        except Exception as e:
            return AgentResult(success=False, error=str(e))
