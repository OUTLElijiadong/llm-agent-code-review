"""Exercise real installer rollback with isolated unit files and fake systemctl."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "systemd"


class SystemdInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.deploy = self.root / "deploy"
        self.units = self.root / "units"
        self.bin = self.root / "bin"
        self.units.mkdir()
        self.bin.mkdir()
        shutil.copytree(SOURCE, self.deploy / "systemd")
        # This copy only runs fake commands against a temporary unit directory.
        installer = self.deploy / "systemd" / "install.sh"
        text = installer.read_text()
        guard = '[[ "$EUID" -eq 0 ]]'
        self.assertIn(guard, text)
        installer.write_text(text.replace(guard, '[[ "1" -eq 1 ]]', 1))
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        TEST_DEPLOY=str(self.deploy), TEST_ROOT=str(self.root))
        (self.root / "before-active").write_text("prism-ops-executor.service\n")
        (self.root / "before-enabled").write_text("prism-ops-executor.service\n")
        self.write_command("getent", '#!/bin/sh\nprintf "prism-ops:x:991:\n"\n')
        self.write_command("groupadd", '#!/bin/sh\nexit 99\n')
        self.write_command("readlink", '#!/bin/sh\nprintf "%s\n" "$TEST_DEPLOY"\n')
        self.write_command("systemctl", r'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
root = Path(os.environ["TEST_ROOT"])
args = sys.argv[1:]
with (root / "commands.log").open("a") as out:
    out.write(" ".join(args) + "\n")
if args[0] in {"is-active", "is-enabled"}:
    unit = args[-1]
    previous = root / ("before-active" if args[0] == "is-active" else "before-enabled")
    sys.exit(0 if previous.exists() and unit in previous.read_text().splitlines() else 1)
if args[0] == "show":
    prop = next(a for a in args if a.startswith("--property=")).split("=", 1)[1]
    deploy = os.environ["TEST_DEPLOY"]
    print({"WorkingDirectory": deploy, "EnvironmentFiles": deploy + "/.env",
           "ExecStart": deploy + "/prism_ops_executor.py", "MainPID": "123"}[prop])
if args[:2] == ["enable", "--now"] and (root / "fail-enable").exists():
    (root / "fail-enable").unlink()
    sys.exit(1)
''')

    def write_command(self, name, code):
        path = self.bin / name
        path.write_text(code)
        path.chmod(0o755)

    def run_installer(self):
        return subprocess.run(["bash", str(self.deploy / "systemd" / "install.sh"),
            "--apply", "--deploy-dir", str(self.deploy), "--unit-dir", str(self.units)],
            env=self.env, capture_output=True, text=True)

    def test_installs_timer_and_binds_service_to_release(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.units / "prism-security-block.service").read_text()
        self.assertIn(str(self.deploy) + "/prism_security_block.py --reconcile", text)
        self.assertIn("EnvironmentFile=" + str(self.deploy) + "/.env", text)
        self.assertNotIn("@DEPLOY_DIR@", text)
        commands = (self.root / "commands.log").read_text()
        self.assertIn("prism-security-block.timer", commands)
        self.assertIn("enable --now", commands)

    def test_failed_timer_enable_restores_old_units_and_timer_state(self):
        old = "[Unit]\nDescription=previous timer\n"
        (self.units / "prism-backup.timer").write_text(old)
        (self.root / "before-active").write_text("prism-ops-executor.service\nprism-backup.timer\n")
        (self.root / "before-enabled").write_text("prism-ops-executor.service\nprism-backup.timer\n")
        (self.root / "fail-enable").touch()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.units / "prism-backup.timer").read_text(), old)
        self.assertFalse((self.units / "prism-security-block.timer").exists())
        self.assertFalse((self.units / "prism-security-block.service").exists())
        commands = (self.root / "commands.log").read_text()
        self.assertIn("stop prism-security-block.timer", commands)
        self.assertIn("disable prism-security-block.timer", commands)
        self.assertIn("enable prism-backup.timer", commands)
        self.assertIn("start prism-backup.timer", commands)
        self.assertIn("restart prism-ops-executor.service", commands)
        self.assertNotIn("start prism-security-block.timer", commands)


if __name__ == "__main__":
    unittest.main()
