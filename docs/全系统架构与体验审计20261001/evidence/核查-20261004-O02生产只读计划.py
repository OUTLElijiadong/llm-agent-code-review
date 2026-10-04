import hashlib,json,os
from datetime import datetime,timezone,timedelta
from pathlib import Path
from app.core.database import engine
EXPECTED_SHA="732f48f6929ceda1585bcae2489a0b01f0f2c5fb"
EXPECTED_SOURCE_HASH="8a62a10ad21493a3b2f8e6e71c3cf18a81363cb142ae399914c2597e2806a46b"
assert os.environ.get("APP_RELEASE")==EXPECTED_SHA
assert hashlib.sha256(Path("app/api/v1/admin_overview.py").read_bytes()).hexdigest()==EXPECTED_SOURCE_HASH
assert engine.url.drivername=="mysql+pymysql"
queries=[
("tool_day_count","EXPLAIN SELECT tool_call_log.agent_code, count(tool_call_log.id) AS cnt FROM tool_call_log WHERE tool_call_log.create_time >= '2026-10-04 00:00:00' AND tool_call_log.create_time < '2026-10-05 00:00:00' GROUP BY tool_call_log.agent_code"),
("tool_day_actions","EXPLAIN SELECT tool_call_log.agent_code, tool_call_log.action, count(tool_call_log.id) AS cnt FROM tool_call_log WHERE tool_call_log.create_time >= '2026-10-04 00:00:00' AND tool_call_log.create_time < '2026-10-05 00:00:00' GROUP BY tool_call_log.agent_code, tool_call_log.action ORDER BY count(tool_call_log.id) DESC, tool_call_log.agent_code ASC, tool_call_log.action ASC"),
("model_day_usage","EXPLAIN SELECT ai_call_log.agent_label, ai_call_log.model_name, count(ai_call_log.id) AS cnt, coalesce(sum(CASE WHEN (coalesce(ai_call_log.total_tokens, 0) >= coalesce(ai_call_log.prompt_tokens, 0) + coalesce(ai_call_log.completion_tokens, 0)) THEN coalesce(ai_call_log.total_tokens, 0) ELSE coalesce(ai_call_log.prompt_tokens, 0) + coalesce(ai_call_log.completion_tokens, 0) END), 0) AS tokens, coalesce(sum(CASE WHEN (ai_call_log.total_tokens IS NULL AND (ai_call_log.prompt_tokens IS NULL OR ai_call_log.completion_tokens IS NULL)) THEN 1 ELSE 0 END), 0) AS unknown_usage_calls FROM ai_call_log WHERE ai_call_log.create_time >= '2026-10-04 00:00:00' AND ai_call_log.create_time < '2026-10-05 00:00:00' AND (ai_call_log.agent_label IS NOT NULL OR ai_call_log.model_name IS NOT NULL) GROUP BY ai_call_log.agent_label, ai_call_log.model_name"),
("recent_tool_activity","EXPLAIN SELECT tool_call_log.agent_code, tool_call_log.tool_code, tool_call_log.action, tool_call_log.resource, tool_call_log.status, tool_call_log.risk_level, tool_call_log.decision, tool_call_log.input_summary, tool_call_log.output_summary, tool_call_log.error, tool_call_log.duration_ms, tool_call_log.policy_decision_id, tool_call_log.approval_id, tool_call_log.copilot_request_id, tool_call_log.project_id, tool_call_log.id, tool_call_log.create_time, tool_call_log.update_time FROM tool_call_log WHERE tool_call_log.create_time >= '2026-10-04 11:24:12.585193' ORDER BY tool_call_log.create_time DESC, tool_call_log.id DESC"),
("login_30day_geo","EXPLAIN SELECT audit_log.ip, count(audit_log.id) AS cnt FROM audit_log WHERE audit_log.action = 'login' AND audit_log.status = 'success' AND audit_log.create_time >= '2026-09-04 11:24:27.585193' AND audit_log.ip IS NOT NULL AND audit_log.ip != '' GROUP BY audit_log.ip ORDER BY count(audit_log.id) DESC LIMIT 200")]
sample_now=datetime.now(timezone.utc)
cutoff_recent=(sample_now-timedelta(seconds=15)).strftime("%Y-%m-%d %H:%M:%S.%f")
cutoff_login=(sample_now-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
queries=[(key,query.replace("2026-10-04 11:24:12.585193",cutoff_recent).replace("2026-09-04 11:24:27.585193",cutoff_login)) for key,query in queries]
for _,query in queries:
 assert query.startswith("EXPLAIN SELECT ") and ";" not in query
result={"checked_at_utc":datetime.now(timezone.utc).isoformat(),"release":"4.0.37","source_sha":EXPECTED_SHA,"source_file":"backend/app/api/v1/admin_overview.py","source_file_sha256":EXPECTED_SOURCE_HASH,"query_shape_compiled_at_utc":"2026-10-04T11:24:27.585193+00:00","sampled_window_at_utc":sample_now.isoformat(),"recent_tool_cutoff_utc":cutoff_recent,"login_30day_cutoff_utc":cutoff_login,"mode":"EXPLAIN only, read-only transaction, no ANALYZE or business query execution","queries":[]}
raw=engine.raw_connection()
try:
 cursor=raw.cursor()
 cursor.execute("SET SESSION MAX_EXECUTION_TIME = 3000")
 cursor.execute("START TRANSACTION READ ONLY")
 cursor.execute("SELECT VERSION()")
 result["mysql_version"]=cursor.fetchone()[0]
 for key,query in queries:
  cursor.execute(query)
  columns=[description[0] for description in cursor.description]
  plans=[dict(zip(columns,row)) for row in cursor.fetchall()]
  result["queries"].append({"name":key,"sql":query,"plans":plans})
 raw.rollback()
finally:
 raw.close()
 engine.dispose()
result["limits"]=["Optimizer estimates, not measured row counts", "Does not prove response time, throughput, lock impact or every date query", "Temporary/filesort or alternate index choice alone is not a confirmed defect"]
print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
