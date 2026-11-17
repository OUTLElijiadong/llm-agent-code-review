"""Run billing integration against disposable local MySQL and a simulated provider."""

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pymysql


def main():
    name = "prism-sandbox-integration-" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    image = "mysql:8.0"
    started = False
    try:
        available = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
        if available.returncode:
            raise RuntimeError("本地缺少 mysql:8.0 镜像")
        result = subprocess.run([
            "docker", "run", "--pull", "never", "--rm", "-d", "--name", name,
            "--label", "prism.alipay-sandbox=ephemeral", "--tmpfs", "/var/lib/mysql:size=1g",
            "-e", "MYSQL_ROOT_PASSWORD=" + password, "-p", "127.0.0.1::3306", image,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("一次性 MySQL 容器启动失败")
        started = True
        result = subprocess.run(["docker", "port", name, "3306/tcp"], capture_output=True, text=True,
                                timeout=10, check=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) != 1 or not lines[0].startswith("127.0.0.1:"):
            raise RuntimeError("一次性 MySQL 未绑定回环地址")
        port = int(lines[0].rsplit(":", 1)[1])
        deadline = time.monotonic() + 60
        while True:
            try:
                connection = pymysql.connect(host="127.0.0.1", port=port, user="root", password=password,
                                             connect_timeout=1)
                connection.close()
                break
            except pymysql.MySQLError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("一次性 MySQL 启动超时") from None
                time.sleep(0.5)
        env = dict(os.environ, PRISM_FRESH_CHAIN_MYSQL_URL=f"mysql+pymysql://root:{password}@127.0.0.1:{port}/",
                   PRISM_FRESH_CHAIN_REQUIRED="1", PRISM_SANDBOX_MYSQL_REQUIRED="1",
                   PRISM_SANDBOX_MYSQL_CONTAINER=name)
        return subprocess.run([
            sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
            "tests/test_mysql_alipay_sandbox_local.py", "--tb=short",
        ], cwd=Path(__file__).resolve().parents[1], env=env, timeout=240, check=False).returncode
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        # No paths, connection strings, or secrets are printed on failures.
        print(f"本地支付集成测试未完成：{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if started:
            subprocess.run(["docker", "stop", "-t", "3", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
