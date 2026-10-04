"""真实 Redis 的双进程隔离计数验证；不展示连接凭据。"""
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def inspect(name):
    return json.loads(subprocess.check_output(["docker", "inspect", name]))[0]


backend = inspect("cr_backend")
redis_container = inspect("cr_redis")
assert redis_container["State"]["Running"]
environment = dict(item.split("=", 1) for item in backend["Config"]["Env"] if "=" in item)
parts = urlsplit(environment["REDIS_URL"])
assert parts.scheme == "redis"
credentials = parts.netloc.rpartition("@")[0]
redis_url = parts._replace(netloc=(credentials + "@" if credentials else "") + "127.0.0.1:6379").geturl()
root = Path(__file__).resolve().parents[3]
env_file = root / "redis-validation.env"
descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(descriptor, "w") as stream:
        stream.write("APP_ENV=dev\nPYTHONPATH=/candidate/backend\n")
        stream.write("AGENT_GOVERNANCE_SCHEDULER_ENABLED=false\nOPS_AUTOMATION_ENABLED=false\n")
        assert "\n" not in redis_url
        stream.write(f"REDIS_URL={redis_url}\nVALIDATION_REDIS_PREFIX=prism:verification:{secrets.token_hex(16)}\n")
    result = subprocess.run([
        "docker", "run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--network", f"container:{redis_container['Id']}", "--memory", "384m", "--cpus", "0.5",
        "--env-file", str(env_file), "--mount", f"type=bind,source={root},target=/candidate,readonly",
        "--tmpfs", "/tmp", "--workdir", "/candidate/backend", "--entrypoint", "python", backend["Config"]["Image"],
        "/candidate/docs/全系统架构与体验审计20261001/evidence/verify_shared_redis.py",
    ], check=False)
    raise SystemExit(result.returncode)
finally:
    env_file.unlink(missing_ok=True)
