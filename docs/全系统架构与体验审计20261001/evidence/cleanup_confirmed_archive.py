"""只删除本轮核验的过期归档；当前备份须已完成独立恢复验收。"""
import hashlib
import json
import subprocess
import time
from pathlib import Path


archive = Path("/opt/code-review/backups/mysql-general-log-20260819T192021")
expected = {"general_log.CSV": 26243998805, "general_log.CSM": 35}
backup = Path("/opt/prism-releases/310ea1a8095428d670c0404a05b4dc643ad0027e/backups/code_review_20261004T081157Z_310ea1a80954.sql.gz")
assert archive.is_dir() and not archive.is_symlink()
assert archive.resolve() == archive
assert {path.name for path in archive.iterdir()} == set(expected)
assert backup.is_file() and backup.with_name(backup.name + ".meta").is_file()
digest = hashlib.sha256()
with backup.open("rb") as stream:
    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
        digest.update(block)
verified_backup_sha256 = "f5bb4386e9ffda2f349e6e07345920f73f706d2ce793faa35dd49930e9d15306"
assert digest.hexdigest() == verified_backup_sha256
assert digest.hexdigest() == backup.with_name(backup.name + ".sha256").read_text().split()[0]
subprocess.run(["gzip", "-t", str(backup)], check=True)
records = []
for name, size in expected.items():
    path = archive / name
    assert path.is_file() and not path.is_symlink()
    stat = path.stat()
    assert stat.st_size == size and time.time() - stat.st_mtime >= 45 * 86400
    opened = subprocess.run(["lsof", "-t", str(path)], capture_output=True, text=True)
    assert opened.returncode == 1 and not opened.stdout.strip() and not opened.stderr.strip(), "archive may be in use"
    records.append({"path": str(path), "bytes": size, "mtime": stat.st_mtime})
containers = json.loads(subprocess.check_output(["docker", "inspect", "cr_mysql", "cr_testdb", "cr_backend", "cr_frontend"]))
for container in containers:
    for mount in container["Mounts"]:
        source = Path(mount["Source"]).resolve()
        assert source != archive and source not in archive.parents and archive not in source.parents, "archive overlaps a live bind mount"
before = subprocess.check_output(["df", "-B1", "/"], text=True)
for record in records:
    Path(record["path"]).unlink()
archive.rmdir()
after = subprocess.check_output(["df", "-B1", "/"], text=True)
assert not archive.exists()
print(json.dumps({"removed": records, "backup_sha256": digest.hexdigest(), "backup": str(backup),
                  "restore_gate": "cr_testdb/prism_verify_20261004081824_24910 103 tables / 058 verified for the fixed backup SHA", "disk_before": before, "disk_after": after}, indent=2))
