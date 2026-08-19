"""ReviewService 状态机、事务、权限与后台执行的隔离覆盖测试。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import PendingRollbackError

from app.agents.events import AgentEventType
from app.core.exceptions import NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import review_service


class CapturingThread:
    """记录后台线程创建参数但不真正启动线程。"""

    created: list["CapturingThread"] = []

    def __init__(self, *, target: Any, args: tuple[Any, ...], name: str, daemon: bool):
        """保存线程目标、参数和守护属性。"""
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        type(self).created.append(self)

    def start(self) -> None:
        """标记线程已请求启动。"""
        self.started = True


class FakeDeepSeekAgent:
    """提供 ReviewService 启动阶段所需的最小模型属性。"""

    def __init__(self, api_config: Any = None):
        """保存 API 配置并暴露固定模型名。"""
        self.api_config = api_config
        self.model = "fake-model"


class CommitRetrySession:
    """模拟第一次提交进入 PendingRollbackError 的数据库会话。"""

    def __init__(self):
        """初始化提交、回滚和 add 调用记录。"""
        self.commit_calls = 0
        self.rollback_calls = 0
        self.added: list[Any] = []

    def add(self, item: Any) -> None:
        """记录被重新加入会话的对象。"""
        self.added.append(item)

    def commit(self) -> None:
        """第一次提交抛出脏会话异常，第二次成功。"""
        self.commit_calls += 1
        if self.commit_calls == 1:
            raise PendingRollbackError("dirty session")

    def rollback(self) -> None:
        """记录回滚次数。"""
        self.rollback_calls += 1


class FakeSemaphore:
    """记录后台审查并发信号量的获取与释放。"""

    def __init__(self):
        """初始化 acquire/release 计数。"""
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        """记录一次信号量获取。"""
        self.acquired += 1

    def release(self) -> None:
        """记录一次信号量释放。"""
        self.released += 1


class FakeFileQuery:
    """为后台入口提供可链式调用的文件查询。"""

    def __init__(self, files: list[Any]):
        """保存最终需要返回的文件列表。"""
        self.files = files

    def join(self, *args: Any, **kwargs: Any) -> "FakeFileQuery":
        """忽略 join 条件并返回自身。"""
        return self

    def filter(self, *args: Any, **kwargs: Any) -> "FakeFileQuery":
        """忽略过滤条件并返回自身。"""
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "FakeFileQuery":
        """忽略排序条件并返回自身。"""
        return self

    def all(self) -> list[Any]:
        """返回预置文件列表。"""
        return self.files


class FakeBackgroundSession:
    """为后台线程入口提供模型查找、文件查询与关闭能力。"""

    def __init__(self, task: Any = None, user: Any = None, project: Any = None, files: list[Any] | None = None):
        """保存各模型返回值和生命周期状态。"""
        self.task = task
        self.user = user
        self.project = project
        self.files = files or []
        self.closed = False

    def get(self, model: Any, object_id: int) -> Any:
        """按 ORM 模型返回预置对象。"""
        if model is ReviewTask:
            return self.task
        if model is User:
            return self.user
        if model is Project:
            return self.project
        return None

    def query(self, model: Any) -> FakeFileQuery:
        """返回预置文件查询对象。"""
        assert model is CodeFile
        return FakeFileQuery(self.files)

    def close(self) -> None:
        """标记会话已关闭。"""
        self.closed = True


class CapturingEventBus:
    """捕获 ReviewService 发布的 AgentEvent。"""

    events: list[Any] = []

    @classmethod
    def instance(cls) -> "CapturingEventBus":
        """返回当前捕获总线实例。"""
        return cls()

    def publish(self, event: Any) -> None:
        """记录发布事件。"""
        type(self).events.append(event)


class FailingEventBus:
    """模拟事件发布失败的总线。"""

    @classmethod
    def instance(cls) -> "FailingEventBus":
        """返回失败总线实例。"""
        return cls()

    def publish(self, event: Any) -> None:
        """始终抛出发布异常。"""
        raise RuntimeError("event bus unavailable")


def _make_user(db: Any, user_id: int = 1, *, role: str = "user") -> User:
    """创建并持久化测试用户。"""
    user = User(
        id=user_id,
        username=f"user-{user_id}",
        password="x",
        email=f"user-{user_id}@example.test",
        role=role,
        status=1,
    )
    db.add(user)
    db.commit()
    return user


def _make_project(db: Any, owner_id: int, project_id: int = 10, *, name: str = "demo") -> Project:
    """创建并持久化测试项目。"""
    project = Project(
        id=project_id,
        user_id=owner_id,
        project_name=f"{name}-{project_id}",
        language="Python",
        status="active",
    )
    db.add(project)
    db.commit()
    return project


def _make_file(db: Any, project_id: int, file_id: int = 100, *, name: str = "app.py") -> CodeFile:
    """创建并持久化活动代码文件。"""
    code_file = CodeFile(
        id=file_id,
        project_id=project_id,
        file_name=name,
        file_path=f"src/{name}",
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        raw_size=12,
        line_count=1,
        version_no=1,
        status="active",
    )
    db.add(code_file)
    db.commit()
    return code_file


def _make_task(
    db: Any,
    user_id: int,
    project_id: int,
    task_id: int = 1000,
    *,
    status: str = "running",
    name: str = "review",
) -> ReviewTask:
    """创建并持久化审查任务。"""
    task = ReviewTask(
        id=task_id,
        user_id=user_id,
        project_id=project_id,
        task_name=f"{name}-{task_id}",
        review_type="standard",
        status=status,
        total_files=1,
        processed_files=0,
        total_issues=0,
        severe_issues=0,
        high_issues=0,
        medium_issues=0,
        low_issues=0,
        score=0,
        duration_ms=0,
    )
    db.add(task)
    db.commit()
    return task


def _patch_start_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换启动流程中的模型、规则、配置与线程外部依赖。"""
    CapturingThread.created.clear()
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(review_service, "DeepSeekAgent", FakeDeepSeekAgent)
    monkeypatch.setattr(review_service, "get_model_label", lambda *args, **kwargs: "fake-model")
    monkeypatch.setattr(review_service.threading, "Thread", CapturingThread)
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *args, **kwargs: {"provider": "fake"})


def _runtime_task() -> SimpleNamespace:
    """构造 _execute_review 所需的可变任务对象。"""
    return SimpleNamespace(
        id=7,
        review_type="standard",
        processed_files=0,
        total_files=1,
        total_issues=0,
        severe_issues=0,
        high_issues=0,
        medium_issues=0,
        low_issues=0,
        score=0,
        summary=None,
        status="running",
        end_time=None,
        duration_ms=0,
        error_message=None,
    )


def test_safe_commit_rolls_back_and_retries_dirty_session() -> None:
    """脏会话提交失败后应回滚、重新 add 任务并再次提交。"""
    session = CommitRetrySession()
    task = SimpleNamespace(id=1)

    review_service._safe_commit(session, task)

    assert session.commit_calls == 2
    assert session.rollback_calls == 1
    assert session.added == [task, task]


def test_start_persists_links_and_starts_daemon_thread(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """合法启动请求应持久化任务和文件关系并启动守护线程。"""
    user = _make_user(db)
    project = _make_project(db, user.id)
    first = _make_file(db, project.id, 101, name="a.py")
    second = _make_file(db, project.id, 102, name="b.py")
    _patch_start_dependencies(monkeypatch)

    task = review_service.start(
        db,
        user,
        ReviewStartIn(project_id=project.id, file_ids=[first.id, second.id], task_name="targeted"),
    )

    links = db.query(ReviewTaskFile).filter(ReviewTaskFile.task_id == task.id).order_by(ReviewTaskFile.file_id).all()
    assert task.status == "running"
    assert task.task_name == "targeted"
    assert task.total_files == 2
    assert [link.file_id for link in links] == [first.id, second.id]
    assert len(CapturingThread.created) == 1
    thread = CapturingThread.created[0]
    assert thread.args == (task.id, user.id, task.execution_token)
    assert thread.daemon is True
    assert thread.started is True


@pytest.mark.parametrize("file_ids", [[], list(range(1, 502))])
def test_start_rejects_invalid_file_count(db: Any, monkeypatch: pytest.MonkeyPatch, file_ids: list[int]) -> None:
    """空列表和超过 500 个文件的请求均应被服务层拒绝。"""
    user = _make_user(db)
    project = _make_project(db, user.id)
    _patch_start_dependencies(monkeypatch)
    payload = ReviewStartIn.model_construct(
        project_id=project.id,
        file_ids=file_ids,
        review_type="standard",
        task_name=None,
    )

    with pytest.raises(ValidationError, match="1-500"):
        review_service.start(db, user, payload)


def test_start_rejects_missing_project_and_partial_files(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """项目不存在或请求文件不全时应返回不可枚举的 NotFoundError。"""
    user = _make_user(db)
    _patch_start_dependencies(monkeypatch)
    with pytest.raises(NotFoundError, match="项目不存在"):
        review_service.start(db, user, ReviewStartIn(project_id=999, file_ids=[1]))

    project = _make_project(db, user.id)
    _make_file(db, project.id, 101)
    with pytest.raises(NotFoundError, match="部分文件不存在"):
        review_service.start(db, user, ReviewStartIn(project_id=project.id, file_ids=[101, 102]))


def test_run_review_task_executes_with_degraded_optional_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """经验和画像加载失败时后台入口仍应执行审查并释放全部资源。"""
    task = SimpleNamespace(id=11, project_id=3, review_type="standard")
    user = SimpleNamespace(id=22)
    project = SimpleNamespace(id=3, language="Python")
    code_file = SimpleNamespace(id=33)
    session = FakeBackgroundSession(task, user, project, [code_file])
    semaphore = FakeSemaphore()
    captured: list[tuple[Any, ...]] = []

    def _raise_experience(*args: Any, **kwargs: Any) -> list[Any]:
        """模拟经验服务不可用。"""
        raise RuntimeError("experience unavailable")

    def _raise_persona(*args: Any, **kwargs: Any) -> str:
        """模拟画像服务不可用。"""
        raise RuntimeError("persona unavailable")

    def _capture_execute(*args: Any, **kwargs: Any) -> None:
        """捕获后台入口传给主执行器的参数。"""
        captured.append(args)

    monkeypatch.setattr(review_service, "SessionLocal", lambda: session)
    monkeypatch.setattr(review_service, "_REVIEW_SEMAPHORE", semaphore)
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *args, **kwargs: ["rule"])
    monkeypatch.setattr(review_service, "get_agent_profiles", lambda review_type: ("profile",))
    monkeypatch.setattr(review_service, "_enabled_review_profiles", lambda db, profiles: profiles)
    monkeypatch.setattr(review_service, "DeepSeekAgent", FakeDeepSeekAgent)
    monkeypatch.setattr(review_service, "_execute_review", _capture_execute)
    monkeypatch.setattr("app.services.experience_service.retrieve", _raise_experience)
    monkeypatch.setattr("app.services.personalization_service.build_review_context", _raise_persona)
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *args, **kwargs: {"provider": "fake"})

    review_service._run_review_task(task.id, user.id)

    assert len(captured) == 1
    assert captured[0][0] is session
    assert captured[0][3] is task
    assert captured[0][5] == [code_file]
    assert captured[0][-1] == ""
    assert session.closed is True
    assert semaphore.acquired == 1
    assert semaphore.released == 1


def test_run_review_task_missing_objects_still_closes_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """后台任务找不到 task/user 时应安全返回并关闭会话、释放信号量。"""
    session = FakeBackgroundSession()
    semaphore = FakeSemaphore()
    monkeypatch.setattr(review_service, "SessionLocal", lambda: session)
    monkeypatch.setattr(review_service, "_REVIEW_SEMAPHORE", semaphore)

    review_service._run_review_task(404, 405)

    assert session.closed is True
    assert semaphore.acquired == 1
    assert semaphore.released == 1


def test_execute_review_success_normalizes_severity_and_emits_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功流程应累计文件、归一化未知严重度、评分并发布完成事件。"""
    task = _runtime_task()
    user = SimpleNamespace(id=9)
    files = [SimpleNamespace(id=1, file_name="a.py")]
    issues = [SimpleNamespace(severity="严重"), SimpleNamespace(severity="unexpected")]
    events: list[tuple[AgentEventType, str]] = []
    commit_calls: list[Any] = []
    clock = iter([10.0, 10.25])

    def _capture_event(
        type_: AgentEventType,
        task_obj: Any,
        user_obj: Any,
        message: str,
        agent_code: str = "review_orchestrator",
    ) -> None:
        """记录事件类型与代理编码。"""
        events.append((type_, agent_code))

    monkeypatch.setattr(review_service.time, "time", lambda: next(clock))
    monkeypatch.setattr(review_service, "_check_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_review_one_file", lambda *args, **kwargs: issues)
    monkeypatch.setattr(review_service, "_safe_commit", lambda db, task_obj=None: commit_calls.append(task_obj))
    monkeypatch.setattr(review_service, "_emit_review_event", _capture_event)
    monkeypatch.setattr(review_service, "compute_score", lambda counts: 77)
    monkeypatch.setattr(review_service, "_build_summary", lambda *args, **kwargs: "summary")

    review_service._execute_review(
        SimpleNamespace(refresh=lambda task_obj: None),
        None,
        None,
        task,
        user,
        files,
        [],
        (SimpleNamespace(code="general"),),
        "experience",
    )

    assert task.status == "success"
    assert task.processed_files == 1
    assert task.total_issues == 2
    assert task.severe_issues == 1
    assert task.medium_issues == 1
    assert task.score == 77
    assert task.summary == "summary"
    assert task.duration_ms == 250
    assert len(commit_calls) == 2
    assert (AgentEventType.PROGRESS, "code_reviewer") in events
    assert (AgentEventType.COMPLETE, "review_orchestrator") in events


def test_execute_review_preserves_cancelled_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消信号应使任务保持 cancelled 并向参与代理发布失败事件。"""
    task = _runtime_task()
    user = SimpleNamespace(id=9)
    events: list[tuple[AgentEventType, str]] = []
    clock = iter([20.0, 20.1])

    def _raise_cancelled(*args: Any, **kwargs: Any) -> None:
        """模拟用户已经取消任务。"""
        raise review_service.TaskCancelledError("cancelled")

    def _capture_event(
        type_: AgentEventType,
        task_obj: Any,
        user_obj: Any,
        message: str,
        agent_code: str = "review_orchestrator",
    ) -> None:
        """记录取消流程发布的事件。"""
        events.append((type_, agent_code))

    monkeypatch.setattr(review_service.time, "time", lambda: next(clock))
    monkeypatch.setattr(review_service, "_check_cancelled", _raise_cancelled)
    monkeypatch.setattr(review_service, "_safe_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_emit_review_event", _capture_event)

    review_service._execute_review(
        SimpleNamespace(),
        None,
        None,
        task,
        user,
        [SimpleNamespace(file_name="a.py")],
        [],
        (SimpleNamespace(code="security"),),
        "",
    )

    assert task.status == "cancelled"
    assert task.duration_ms == 100
    assert (AgentEventType.FAILED, "security_sentinel") in events


def test_execute_review_records_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """文件审查异常应落库 failed、截断错误并发布失败事件。"""
    task = _runtime_task()
    user = SimpleNamespace(id=9)
    events: list[AgentEventType] = []
    clock = iter([30.0, 30.2])

    def _raise_failure(*args: Any, **kwargs: Any) -> list[Any]:
        """模拟文件审查器崩溃。"""
        raise RuntimeError("review exploded")

    def _capture_event(type_: AgentEventType, *args: Any, **kwargs: Any) -> None:
        """记录失败流程事件类型。"""
        events.append(type_)

    monkeypatch.setattr(review_service.time, "time", lambda: next(clock))
    monkeypatch.setattr(review_service, "_check_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_review_one_file", _raise_failure)
    monkeypatch.setattr(review_service, "_safe_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_service, "_emit_review_event", _capture_event)

    review_service._execute_review(
        SimpleNamespace(),
        None,
        None,
        task,
        user,
        [SimpleNamespace(file_name="bad.py")],
        [],
        (SimpleNamespace(code="general"),),
        "",
    )

    assert task.status == "failed"
    assert task.error_message == "review exploded"
    assert task.duration_ms == 199
    assert AgentEventType.FAILED in events


def test_task_queries_cover_visibility_details_filters_and_file_fallback(db: Any) -> None:
    """任务列表、详情、问题过滤和旧任务文件回退应返回一致数据。"""
    user = _make_user(db)
    project = _make_project(db, user.id)
    code_file = _make_file(db, project.id)
    task = _make_task(db, user.id, project.id, status="success")
    linked_task = _make_task(db, user.id, project.id, task_id=1001, status="success", name="linked")
    deleted_task = _make_task(db, user.id, project.id, task_id=1002, status="deleted", name="deleted")
    db.add(ReviewTaskFile(task_id=linked_task.id, file_id=code_file.id))
    db.add_all([
        ReviewIssue(
            task_id=task.id,
            file_id=code_file.id,
            file_name=code_file.file_name,
            issue_type="安全漏洞",
            severity="高",
            title="SQL injection",
            description="unsafe query",
            status="unfixed",
        ),
        ReviewIssue(
            task_id=task.id,
            file_id=code_file.id,
            file_name=code_file.file_name,
            issue_type="代码规范",
            severity="低",
            title="style",
            description="style issue",
            status="fixed",
        ),
    ])
    db.commit()

    page = review_service.list_tasks(db, user, project_id=project.id, status="success", page=1, page_size=20)
    detail = review_service.get_task_detail(db, user, linked_task.id)
    default_issues = review_service.list_task_issues(db, user, task.id)
    all_issues = review_service.list_task_issues(db, user, task.id, status="all", severity="低")
    fallback_files = review_service._task_file_summaries(db, task.id)

    assert page["total"] == 2
    assert {item["id"] for item in page["items"]} == {task.id, linked_task.id}
    assert all(item["project_name"] == project.project_name for item in page["items"])
    assert detail["files"][0]["file_id"] == code_file.id
    assert default_issues["total"] == 1
    assert default_issues["items"][0].status == "unfixed"
    assert all_issues["total"] == 1
    assert all_issues["items"][0].status == "fixed"
    assert fallback_files[0]["file_id"] == code_file.id
    with pytest.raises(NotFoundError):
        review_service.get_task_detail(db, user, deleted_task.id)


def test_delete_cancel_and_cancel_check_enforce_task_state(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除、取消与运行时取消检查应更新状态并拒绝非法状态。"""
    user = _make_user(db)
    project = _make_project(db, user.id)
    running = _make_task(db, user.id, project.id)
    finished = _make_task(db, user.id, project.id, task_id=1001, status="success")
    access_modes: list[bool] = []

    def _allow_access(db_obj: Any, project_id: int, user_obj: Any, need_write: bool = False) -> str:
        """记录权限检查模式并允许访问。"""
        access_modes.append(need_write)
        return "owner"

    monkeypatch.setattr("app.services.project_member_service.require_project_access", _allow_access)

    review_service.cancel_task(db, user, running.id)
    assert running.status == "cancelled"
    with pytest.raises(review_service.TaskCancelledError):
        review_service._check_cancelled(db, running)
    with pytest.raises(ValidationError, match="只能取消"):
        review_service.cancel_task(db, user, finished.id)
    review_service.delete_task(db, user, finished.id)
    assert finished.status == "deleted"
    assert access_modes == [True, True, True]

    with pytest.raises(NotFoundError):
        review_service.cancel_task(db, user, 99999)
    with pytest.raises(NotFoundError):
        review_service.delete_task(db, user, 99999)


def test_emit_review_event_includes_user_scope_and_swallows_bus_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查事件应携带用户隔离字段，且总线故障不得中断审查。"""
    task = SimpleNamespace(id=71)
    user = SimpleNamespace(id=81)
    CapturingEventBus.events.clear()
    monkeypatch.setattr(review_service, "AgentEventBus", CapturingEventBus)
    monkeypatch.setattr(review_service.time, "time", lambda: 123.456)

    review_service._emit_review_event(AgentEventType.PROGRESS, task, user, "working", agent_code="security_sentinel")

    event = CapturingEventBus.events[0]
    assert event.type is AgentEventType.PROGRESS
    assert event.agent == "security_sentinel"
    assert event.trace_id == "review_71_123456"
    assert event.payload == {"task_id": 71, "user_id": 81}
    assert event.user_id == 81

    monkeypatch.setattr(review_service, "AgentEventBus", FailingEventBus)
    review_service._emit_review_event(AgentEventType.FAILED, task, user, "ignored failure")
