"""独立隔离检查；使用内存库，不登录或修改生产，不调用模型。"""

import asyncio
import json
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import agent_responses, forum
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.forum_post import ForumPost
from app.models.user import User
from app.services import forum_service


def check():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    actor = User(username="isolated-contract-owner", password="unused", role="user", status=1)
    db.add(actor)
    db.flush()
    when = datetime(2026, 10, 14)
    posts = [ForumPost(user_id=actor.id, title=f"保留样本{i}", content="本地隔离", category="tech",
                       status="normal", is_pinned=(i == 0),
                       create_time=when - timedelta(days=1) if i == 0 else when) for i in range(5)]
    posts.append(ForumPost(user_id=actor.id, title="已删除样本", content="本地隔离", category="tech",
                           status="deleted", create_time=when + timedelta(days=1)))
    db.add_all(posts)
    db.commit()
    ids = [post.id for post in posts]
    assert forum_service.list_posts(db)["items"][0]["id"] == ids[0]
    filtered = forum_service.list_posts(db, keyword="保留", category="tech", sort="newest", page_size=4)
    assert [post["id"] for post in filtered["items"]] == ids[4:0:-1]
    app = FastAPI()
    register_handlers(app)
    app.include_router(forum.router, prefix="/api/forum")
    app.dependency_overrides[get_db] = lambda: db
    results = [{"scenario": "默认置顶及newest组合过滤", "passed": True}]

    async def check_routes():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
            response = await client.get("/api/forum/posts", params={"sort": "newest"})
            assert response.status_code == 401
            results.append({"scenario": "真实鉴权依赖无认证请求", "http_status": response.status_code})
            app.dependency_overrides[get_current_user] = lambda: actor
            # 使用项目注册的全局参数错误合同400，而不是FastAPI原生默认422。
            for sort, code in [("newest", 200), ("default", 200), ("created desc; drop table", 400),
                               ("NEWEST", 400), ("", 400)]:
                response = await client.get("/api/forum/posts", params={"sort": sort, "page_size": 2})
                assert response.status_code == code, (sort, response.status_code, response.text)
                results.append({"scenario": "隔离认证替身sort参数合同", "sort": sort,
                                "http_status": response.status_code})
            response = await client.get("/api/forum/posts", params={"sort": "newest", "page": 2, "page_size": 2})
            assert response.status_code == 200
            assert [post["id"] for post in response.json()["data"]["items"]] == ids[2:0:-1]
            results.append({"scenario": "newest第二页同时间记录稳定", "http_status": 200})

    asyncio.run(check_routes())
    for has_ledger in [True, False]:
        run_id = "redaction-ledger" if has_ledger else "redaction-transcript"
        raw_error = "Authorization: Bearer isolated-bearer-marker api_key=isolated-api-marker"
        result = {"status": "success", "output": {"status": "failed", "completed": False, "error": raw_error}}
        checkpoint = {"transcript": [
            {"type": "function_call", "call_id": "browser-call", "name": "operate_current_page", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "browser-call", "output": json.dumps(result)},
        ]}
        run = AgentResponseRun(run_id=run_id, user_id=actor.id, surface="user", session_key="isolated-contract-session",
                               status="completed", checkpoint_json=json.dumps(checkpoint))
        db.add(run)
        db.flush()
        if has_ledger:
            db.add(AgentToolExecution(request_id=run_id, run_id=run_id, user_id=actor.id, call_id="browser-call",
                                     tool_name="operate_current_page", status="success", arguments_json="{}",
                                     result_json=json.dumps(result)))
        db.commit()
        events = agent_responses._public_completed_tool_events(db, run, checkpoint)
        assert events[-1]["type"] == "response.tool.failed"
        serialized = json.dumps(events)
        assert "isolated-bearer-marker" not in serialized and "isolated-api-marker" not in serialized
        results.append({"scenario": "浏览器旧失败回执内部错误脱敏", "ledger": has_ledger, "secret_redacted": True})
    db.close()
    engine.dispose()
    return {"local_isolated_only": True, "production_mutations": False, "results": results}


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False, indent=2))
