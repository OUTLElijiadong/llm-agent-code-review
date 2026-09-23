"""文件名关键词搜索应按用户输入的字面字符匹配。"""

from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import code_file_service


def test_list_files_treats_sql_like_wildcards_as_literal(db):
    user = User(id=1, username="keyword_owner", password="x", role="admin", status=1)
    project = Project(id=1, user_id=1, project_name="keyword_project", status="active", language="python")
    db.add_all([user, project])
    db.add_all(
        [
            CodeFile(project_id=1, file_name="rate%check.py", language="python", content="pass"),
            CodeFile(project_id=1, file_name="snake_case.py", language="python", content="pass"),
            CodeFile(project_id=1, file_name="plain.py", language="python", content="pass"),
        ]
    )
    db.commit()

    percent = code_file_service.list_files(db, user, project_id=1, keyword="%")
    underscore = code_file_service.list_files(db, user, project_id=1, keyword="_")

    assert [item.file_name for item in percent["items"]] == ["rate%check.py"]
    assert [item.file_name for item in underscore["items"]] == ["snake_case.py"]
