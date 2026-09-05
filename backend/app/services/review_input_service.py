from hashlib import sha256
from types import SimpleNamespace

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_task_file import ReviewTaskFile


def validate_review_input(code_file: CodeFile) -> None:
    if getattr(code_file, "is_binary", 0) == 1 or not (code_file.content or "").strip():
        raise ValidationError(f"文件 {code_file.file_name} 没有有效非空文本，不能进行代码扫描", code=40001)


def freeze_task_inputs(db: Session, task_id: int, files: list[CodeFile]) -> None:
    for code_file in files:
        validate_review_input(code_file)
        version = db.query(CodeVersion).filter_by(file_id=code_file.id, version_no=code_file.version_no).one_or_none()
        if version is None or version.content != code_file.content:
            raise ValidationError(f"文件 {code_file.file_name} 的版本记录不一致，请保存后重新扫描", code=40001)
        db.add(ReviewTaskFile(
            task_id=task_id,
            file_id=code_file.id,
            version_no=version.version_no,
            content_sha256=sha256(version.content.encode("utf-8")).hexdigest(),
            file_snapshot={
                "project_id": code_file.project_id,
                "file_name": code_file.file_name,
                "file_path": code_file.file_path,
                "language": code_file.language,
                "line_count": code_file.line_count,
            },
        ))


def load_task_inputs(db: Session, task_id: int) -> list[SimpleNamespace]:
    links = db.query(ReviewTaskFile).filter_by(task_id=task_id).order_by(ReviewTaskFile.id).all()
    if not links:
        raise ValidationError("任务缺少有效扫描输入，请重新创建扫描", code=40001)
    files = []
    for link in links:
        if not link.version_no or not link.content_sha256 or not link.file_snapshot:
            raise ValidationError("历史任务缺少可信版本快照，不能自动改用当前内容；请重新创建扫描", code=40001)
        version = db.query(CodeVersion).filter_by(file_id=link.file_id, version_no=link.version_no).one_or_none()
        if version is None or sha256(version.content.encode("utf-8")).hexdigest() != link.content_sha256:
            raise ValidationError("扫描输入版本缺失或内容校验失败，请重新创建扫描", code=40001)
        code_file = SimpleNamespace(id=link.file_id, content=version.content, version_no=link.version_no,
                                   is_binary=0, **link.file_snapshot)
        validate_review_input(code_file)
        files.append(code_file)
    return files


def verified_task_input_ids(db: Session, task_id: int) -> set[int]:
    database_hashing = db.get_bind().dialect.name == "mysql"
    digest = func.sha2(CodeVersion.content, 256) if database_hashing else CodeVersion.content
    rows = db.query(ReviewTaskFile.id, ReviewTaskFile.content_sha256, digest).join(
        CodeVersion,
        and_(CodeVersion.file_id == ReviewTaskFile.file_id, CodeVersion.version_no == ReviewTaskFile.version_no),
    ).filter(ReviewTaskFile.task_id == task_id, ReviewTaskFile.content_sha256.isnot(None)).all()
    return {
        link_id for link_id, expected, value in rows
        if value is not None
        and expected == (value if database_hashing else sha256(value.encode("utf-8")).hexdigest())
    }
