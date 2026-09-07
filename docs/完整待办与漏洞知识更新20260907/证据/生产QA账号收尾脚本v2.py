#!/usr/bin/env python3
"""Finite QA account finalization. Default is a local plan; --execute needs prior authorization."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "https://lijiadong.cn"
MARKER = "20260907"
ROLE_CODE = "qa_permission_20260907"
CREDENTIALS = Path("/root/prism-acceptance-20260907/credentials.json")
RESULT = CREDENTIALS.parent / "finalize-qa-result-v2.json"
ACCOUNTS = {"owner_a": 103, "member_a": 104, "owner_b": 105, "no_permission": 106}
PREPARE_SHA256 = "18d5ccb87620c9366afd185bf4b416aa906bed434a670323ca6c2bd9fc1f03b9"


class FinalizationError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise FinalizationError(code)


def run(command, *, input=None, timeout=45):
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise FinalizationError("command_unavailable_or_outcome_uncertain") from None
    require(result.returncode == 0, "command_failed_or_outcome_uncertain")
    return result.stdout


def validate_manifest(manifest):
    require(isinstance(manifest, dict), "invalid_manifest")
    require(manifest.get("marker") == MARKER and manifest.get("role_code") == ROLE_CODE, "marker_or_role_mismatch")
    require(type(manifest.get("role_id")) is int and manifest["role_id"] > 0, "invalid_role_id")
    require(isinstance(manifest.get("accounts"), dict) and set(manifest["accounts"]) == set(ACCOUNTS), "account_set_mismatch")
    for name, user_id in ACCOUNTS.items():
        item = manifest["accounts"][name]
        require(isinstance(item, dict), "invalid_account")
        require(type(item.get("id")) is int and item["id"] == user_id, "account_id_mismatch")
        require(item.get("username") == f"qa_{MARKER}_{name}", "account_name_mismatch")
        require(isinstance(item.get("password"), str) and 16 <= len(item["password"]) <= 128, "invalid_password_shape")
    return manifest


def read_manifest():
    require(os.geteuid() == 0, "root_required")
    parent = CREDENTIALS.parent.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and stat.S_IMODE(parent.st_mode) & 0o077 == 0, "credential_directory_not_private")
    fd = os.open(CREDENTIALS, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600, "credential_file_not_root_0600")
        raw = stream.read(65537)
    require(len(raw) <= 65536, "credential_file_too_large")
    return validate_manifest(json.loads(raw))


def snapshot(role_id):
    # Only constant IDs plus a validated integer; no password or token enters SQL.
    require(type(role_id) is int and role_id > 0, "invalid_role_id")
    sql = f"""
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT 'context', JSON_OBJECT('database',DATABASE(),'readonly',@@session.transaction_read_only);
SELECT 'users', COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id',id,'username',username,'role',role,'status',status,'token_version',token_version)),JSON_ARRAY()) FROM `user` WHERE id IN (103,104,105,106);
SELECT 'role', COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id',id,'code',code,'status',status,'is_builtin',is_builtin)),JSON_ARRAY()) FROM `role` WHERE id={role_id} OR code='qa_permission_20260907';
SELECT 'assignments', COALESCE(JSON_ARRAYAGG(JSON_OBJECT('user_id',ur.user_id,'role_id',ur.role_id,'code',r.code)),JSON_ARRAY()) FROM user_role ur LEFT JOIN `role` r ON r.id=ur.role_id WHERE ur.user_id IN (103,104,105,106);
SELECT 'projects', COALESCE(JSON_ARRAYAGG(JSON_OBJECT('id',id,'user_id',user_id,'marker_match',project_name=CASE id WHEN 162 THEN 'QA权限验收-20260907-a' WHEN 163 THEN 'QA权限验收-20260907-b' END,'status',status,'row_sha256',SHA2(CAST(JSON_ARRAY(id,user_id,project_name,description,language,status,create_time,update_time) AS CHAR CHARACTER SET utf8mb4),256))),JSON_ARRAY()) FROM project WHERE id IN (162,163);
SELECT 'counts', JSON_OBJECT('ai_call_log',(SELECT COUNT(*) FROM ai_call_log WHERE user_id IN (103,104,105,106)),'review_task',(SELECT COUNT(*) FROM review_task WHERE user_id IN (103,104,105,106) OR project_id IN (162,163)));
ROLLBACK;
"""
    raw = run(["docker", "exec", "-i", "cr_mysql", "sh", "-c", 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --default-character-set=utf8mb4 --protocol=TCP -h127.0.0.1 -uroot --database=code_review --batch --raw --skip-column-names'], input=sql.encode())
    parsed = {}
    for line in raw.decode().splitlines():
        key, value = line.split("\t", 1)
        require(key not in parsed, "duplicate_snapshot_key")
        parsed[key] = json.loads(value)
    require(set(parsed) == {"context", "users", "role", "assignments", "projects", "counts"}, "incomplete_snapshot")
    require(parsed["context"] == {"database": "code_review", "readonly": 1}, "snapshot_not_read_only")
    for key in ("users", "role", "projects"):
        parsed[key].sort(key=lambda row: row["id"])
    parsed["assignments"].sort(key=lambda row: (row["user_id"], row["role_id"]))
    return parsed


def validate_snapshot(value, manifest, *, status):
    require(len(value["users"]) == 4, "account_count_changed")
    for row, (name, user_id) in zip(value["users"], ACCOUNTS.items()):
        require(row["id"] == user_id and row["username"] == manifest["accounts"][name]["username"] and row["role"] == "user", "database_identity_changed")
        require(row["status"] == status and type(row["token_version"]) is int and row["token_version"] >= 0, "account_status_or_version_changed")
    require(value["role"] == [{"id": manifest["role_id"], "code": ROLE_CODE, "status": "active", "is_builtin": 0}], "database_role_changed")
    expected = [{"user_id": user_id, "role_id": manifest["role_id"], "code": ROLE_CODE} for name, user_id in ACCOUNTS.items() if name != "no_permission"]
    require(value["assignments"] == expected, "account_roles_changed")
    require(len(value["projects"]) == 2, "qa_project_missing")
    for row, project_id, owner_id in zip(value["projects"], (162, 163), (103, 105)):
        require(row["id"] == project_id and row["user_id"] == owner_id and row["marker_match"] == 1 and row["status"] == "active", "qa_project_identity_changed")
        require(isinstance(row["row_sha256"], str) and len(row["row_sha256"]) == 64, "invalid_project_hash")
    require(value["counts"] == {"ai_call_log": 0, "review_task": 0}, "qa_model_or_review_work_exists")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(client, method, path, *, payload=None, token=None):
    require((method, path) in {("POST", "/api/auth/login"), ("GET", "/api/auth/me")}, "http_operation_not_allowed")
    headers = {"Accept": "application/json", "X-Request-Id": "qa-finalize-20260907"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        response = client.open(urllib.request.Request(BASE + path, body, headers, method=method), timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    except Exception:
        raise FinalizationError("http_transport_failed_no_retry") from None
    with response:
        raw = response.read(65537)
        require(len(raw) <= 65536, "http_body_too_large")
        status_code = response.code
    # Bodies/tokens are never placed in the result or exception text.
    try:
        parsed = json.loads(raw)
        require(isinstance(parsed, dict), "http_body_not_object")
    except (ValueError, TypeError):
        raise FinalizationError("http_body_not_json") from None
    return status_code, parsed


def verify_identity(data, account):
    require(isinstance(data, dict) and data.get("id") == account["id"] and data.get("username") == account["username"] and data.get("role") == "user" and data.get("status") == 1, "http_identity_mismatch")


# Root-only temporary copy inside the existing backend container, removed on all ordinary exits.
# stdout from the existing CLI is captured and reduced to its declared status and IDs.
DISABLE_WRAPPER = r'''
import hashlib,json,os,pathlib,shutil,stat,subprocess,sys,tempfile
folder=None
try:
    if os.geteuid()!=0: raise RuntimeError()
    script=pathlib.Path('/app/scripts/prepare_permission_acceptance.py')
    if hashlib.sha256(script.read_bytes()).hexdigest()!='18d5ccb87620c9366afd185bf4b416aa906bed434a670323ca6c2bd9fc1f03b9': raise RuntimeError()
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536: raise RuntimeError()
    folder=tempfile.mkdtemp(prefix='prism-qa-finalize-',dir='/tmp')
    os.chmod(folder,0o700)
    path=pathlib.Path(folder)/'credentials.json'
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream: stream.write(raw)
    if stat.S_IMODE(path.stat().st_mode)!=0o600 or path.stat().st_uid!=0: raise RuntimeError()
    done=subprocess.run([sys.executable,str(script),'--marker','20260907','--credentials',str(path),'--disable'],capture_output=True,timeout=60)
    if done.returncode!=0: raise RuntimeError()
    result=json.loads(done.stdout)
    if result!={'status':'disabled','account_ids':[103,104,105,106]}: raise RuntimeError()
    print(json.dumps(result))
except Exception:
    print(json.dumps({'status':'failed','error_code':'disable_failed_or_outcome_uncertain'}))
    sys.exit(1)
finally:
    if folder is not None: shutil.rmtree(folder)
'''


def disable(manifest):
    raw = run(["docker", "exec", "-i", "--user", "0", "--workdir", "/app", "cr_backend", "python", "-c", DISABLE_WRAPPER], input=json.dumps(manifest).encode(), timeout=75)
    require(json.loads(raw) == {"status": "disabled", "account_ids": list(ACCOUNTS.values())}, "disable_result_invalid")


def finalize(manifest, client, report, save):
    initial = snapshot(manifest["role_id"])
    validate_snapshot(initial, manifest, status=1)
    report["stage"] = "validated_four_qa_accounts"
    save()
    tokens = {}
    for name, user_id in ACCOUNTS.items():
        account = manifest["accounts"][name]
        code, response = http(client, "POST", "/api/auth/login", payload={"username": account["username"], "password": account["password"]})
        require(code == 200 and response.get("code") == 0, "normal_login_failed_no_retry")
        data = response.get("data")
        require(isinstance(data, dict), "login_data_invalid")
        verify_identity(data.get("user"), account)
        token = data.get("access_token")
        require(isinstance(token, str) and len(token) > 20, "login_token_invalid")
        tokens[name] = token
        code, response = http(client, "GET", "/api/auth/me", token=token)
        require(code == 200 and response.get("code") == 0, "before_disable_me_not_200")
        verify_identity(response.get("data"), account)
        report["accounts"].append({"id": user_id, "login_http": 200, "before_me_http": 200})
        save()
    before = snapshot(manifest["role_id"])
    validate_snapshot(before, manifest, status=1)
    require(before["projects"] == initial["projects"], "project_changed_during_login")
    require(all(b["token_version"] == a["token_version"] + 1 for a, b in zip(initial["users"], before["users"])), "login_version_not_exactly_plus_one")
    report["stage"] = "disable_about_to_run_once"
    save()  # Durable intent; this script never retries the mutation.
    disable(manifest)
    report["stage"] = "disable_returned_success"
    save()
    after = snapshot(manifest["role_id"])
    validate_snapshot(after, manifest, status=0)
    require(after["projects"] == before["projects"], "project_changed_during_disable")
    for entry, old, new in zip(report["accounts"], before["users"], after["users"]):
        require(new["token_version"] == old["token_version"] + 1, "disable_version_not_exactly_plus_one")
        entry.update(status=0, token_version_before=old["token_version"], token_version_after=new["token_version"], token_version_delta=1)
    for name, entry in zip(ACCOUNTS, report["accounts"]):
        code, response = http(client, "GET", "/api/auth/me", token=tokens[name])
        require(code == 403 and response.get("code") == 40301, "disabled_old_token_not_forbidden")
        entry.update(old_token_me_http=code, old_token_me_business_code=40301)
        save()
    tokens.clear()
    report.update(status="passed", stage="finished", model_and_review_counts=after["counts"], retained_projects=after["projects"], credentials_logged=False, model_requests=0, disabled_account_ids=list(ACCOUNTS.values()))
    save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Execute only after the main agent explicitly authorizes finalization.")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "prepared_not_executed", "account_ids": list(ACCOUNTS.values()), "base_url": BASE, "old_token_expected_http": 403, "old_token_expected_business_code": 40301, "retained_project_ids": [162, 163]}))
        return 0
    report = {"status": "running", "stage": "read_manifest", "marker": MARKER, "accounts": [], "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    stream = None
    try:
        manifest = read_manifest()
        # Exclusive root-0600 evidence is also a no-replay guard. Failure requires explicit inspection.
        descriptor = os.open(RESULT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(descriptor, "w")
        def save():
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        finalize(manifest, client, report, save)
    except Exception as error:
        report.update(status="failed", error_code=str(error) if isinstance(error, FinalizationError) else "finalization_failed_inspect_stage_no_retry", error_type=type(error).__name__)
        if stream is not None:
            try:
                save()
            except Exception:
                pass
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        if stream is not None:
            stream.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
