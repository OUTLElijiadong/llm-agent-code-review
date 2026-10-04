"""Regression probe: certificate inspection must follow the mounted Compose directory."""
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("audit_ops_executor", ROOT / "deploy/prism_ops_executor.py")
executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(executor)
commands = []

def fake_run(args, **kwargs):
    commands.append(args)
    return {"exit_code": 0, "stdout": "notAfter=Dec 29 2026", "stderr": ""}

expected_conf = "/persistent/certbot/conf"
with patch.dict(os.environ, {"CERTBOT_CONF_DIR": expected_conf}), patch.object(executor, "DEPLOY_DIR", Path("/release/deploy")), patch.object(
    executor, "_read_env", lambda key: {"APP_DOMAIN": "example.invalid", "CERTBOT_CONF_DIR": expected_conf}.get(key, "")
), patch.object(executor, "run", fake_run):
    executor.execute("certificate_status", {})
actual = commands[0][-1]
expected = expected_conf + "/live/example.invalid/fullchain.pem"
assert actual == expected
print(json.dumps({"configured_path": expected, "executor_path": actual, "uses_configured_certificate_directory": True}, indent=2))
