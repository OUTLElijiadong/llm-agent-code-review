from test_fresh_database_chain import fresh_chain_url, _connect, _execute_sql_file, _alembic_upgrade, INIT_SQL, SEED_SQL, DATABASE
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.services import enterprise_assistant_clarification_service as clarity, enterprise_org_service as org


def test_115_history_survives_upgrade_but_cannot_authorize(fresh_chain_url, monkeypatch):
    """Real 117 database with a 115 decision, upgraded on disposable MySQL only."""
    url = fresh_chain_url
    admin = _connect(url)
    created = False
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s", (DATABASE,))
            assert cursor.fetchone()[0] == 0, "refuse existing database"
        created = True
        _execute_sql_file(admin, INIT_SQL)
        _execute_sql_file(admin, SEED_SQL)
        _alembic_upgrade(url, "117_wechat_fund_statement")
        engine = create_engine(url.set(drivername="mysql+pymysql", database=DATABASE))
        statements=[
        "INSERT INTO user(id,username,password,role,status) VALUES (991001,'legacy-owner','x','user',1),(991002,'legacy-member','x','user',1)",
        "INSERT INTO enterprise(id,name,owner_id,seat_limit,status,paid_order_id,create_time,update_time) VALUES (992001,'legacy',991001,5,'active','legacy-paid',NOW(),NOW())",
        "INSERT INTO enterprise_member(enterprise_id,user_id,role,status,create_time,update_time) VALUES (992001,991001,'owner','active',NOW(),NOW()),(992001,991002,'member','active',NOW(),NOW())",
        "INSERT INTO enterprise_conversation(id,enterprise_id,kind,conversation_key,created_by,create_time,update_time) VALUES (993001,992001,'company','company',991001,NOW(),NOW())",
        "INSERT INTO enterprise_chat_message(id,enterprise_id,conversation_id,sender_id,sender_type,client_message_id,content,create_time,update_time) VALUES (994001,992001,993001,991001,'human','msg-1','不要执行',NOW(),NOW()),(994002,992001,993001,991002,'human','msg-2','@小菱 执行',NOW(),NOW())",
        "INSERT INTO enterprise_assistant_run(id,public_id,request_key,enterprise_id,conversation_id,initiator_user_id,source_message_id,prompt_sha256,status,create_time,update_time) VALUES (995001,'legacy-run','legacy-run',992001,993001,991002,994002,REPEAT('a',64),'running',NOW(),NOW())",
        "INSERT INTO enterprise_assistant_runtime_state(assistant_run_id,enterprise_id,conversation_id,initiator_user_id,source_message_id,status,checkpoint_json,permission_snapshot_json,permission_snapshot_sha256) VALUES (995001,992001,993001,991002,994002,'running','{}','{}',REPEAT('b',64))",
        "INSERT INTO enterprise_assistant_clarification(id,assistant_run_id,enterprise_id,conversation_id,source_message_id,initiator_user_id,phase,call_id,action,target_json,arguments_json,arguments_sha256,authority_sha256,version,status,expires_at,create_time,update_time) VALUES (996001,995001,992001,993001,994002,991002,'tool','old','create','{}','{}',REPEAT('c',64),REPEAT('d',64),1,'pending',DATE_ADD(NOW(),INTERVAL 1 DAY),NOW(),NOW())",
        "INSERT INTO enterprise_assistant_clarification_decision(request_id,enterprise_id,conversation_id,source_message_id,author_user_id,decided_by,author_rank,decider_rank,decision,decided_at,create_time,update_time) VALUES (996001,992001,993001,994001,991001,991001,4,4,'allow',NOW(),NOW(),NOW())"]
        with engine.begin() as db:
            for sql in statements:
                db.execute(text(sql))
        engine.dispose()
        _alembic_upgrade(url, "head")
        engine = create_engine(url.set(drivername="mysql+pymysql", database=DATABASE))
        monkeypatch.setattr(org, "_billing_access_suspended", lambda *args, **kwargs: False)
        with Session(engine) as db:
            assert clarity.status(db, enterprise_id=992001, conversation_id=993001, request_id=996001) == "stale"
            assert db.execute(text("SELECT decision FROM enterprise_assistant_clarification_decision WHERE request_id=996001")).scalar_one() == "allow"
        engine.dispose()
    finally:
        if created:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
        admin.close()
