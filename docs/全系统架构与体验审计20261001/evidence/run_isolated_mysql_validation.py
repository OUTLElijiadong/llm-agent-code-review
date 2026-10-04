"""在服务器调用独立测试库；凭据只放入0600临时文件，最终删除。"""
import json
import os
import secrets
import subprocess
from pathlib import Path


def inspect(name):
    return json.loads(subprocess.check_output(["docker", "inspect", name]))[0]


test_db = inspect("cr_testdb")
production = inspect("cr_mysql")
assert test_db["Id"] != production["Id"]
assert test_db["State"]["Running"]
assert not test_db["HostConfig"]["PortBindings"], "test DB must not expose a host port"
assert {mount["Source"] for mount in test_db["Mounts"]}.isdisjoint({mount["Source"] for mount in production["Mounts"]})
image = inspect("cr_backend")["Config"]["Image"]
db_env = dict(item.split("=", 1) for item in test_db["Config"]["Env"] if "=" in item)
password = db_env["MYSQL_ROOT_PASSWORD"]
assert "\n" not in password
root = Path(__file__).resolve().parents[3]
env_file = root / "validation.env"
values = {
    "APP_ENV": "dev", "DB_HOST": "127.0.0.1", "DB_PORT": "3306", "DB_USER": "root",
    "DB_NAME": "prism_validation_" + secrets.token_hex(6), "DB_PASSWORD": password,
    "INITIAL_ADMIN_PASSWORD": secrets.token_urlsafe(18), "PYTHONPATH": "/candidate/backend",
    "AGENT_GOVERNANCE_SCHEDULER_ENABLED": "false", "OPS_AUTOMATION_ENABLED": "false",
}
descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    result = subprocess.run([
        "docker", "run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--network", f"container:{test_db['Id']}", "--memory", "512m", "--cpus", "0.75",
        "--env-file", str(env_file), "--mount", f"type=bind,source={root},target=/candidate,readonly",
        "--tmpfs", "/tmp", "--workdir", "/candidate/backend", "--entrypoint", "python", image,
        "/candidate/docs/全系统架构与体验审计20261001/evidence/verify_candidate_mysql.py",
    ], check=False)
    raise SystemExit(result.returncode)
finally:
    env_file.unlink(missing_ok=True)
