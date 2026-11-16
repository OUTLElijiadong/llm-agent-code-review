from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "deploy/lib/common.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _assert_clean(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; log_warn() { :; }; assert_deploy_sources_clean "$2"',
            "test-assert-deploy-sources-clean",
            str(COMMON),
            str(repo),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_ops_executor_is_part_of_the_deploy_clean_source_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")

    for relative in (
        "VERSION",
        "backend/app.py",
        "frontend/app.js",
        "deploy/docker-compose.yml",
        "deploy/lib/common.sh",
        "deploy/deploy.sh",
        "deploy/prism_ops_executor.py",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tracked\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    assert _assert_clean(repo).returncode == 0

    (repo / "deploy/prism_ops_executor.py").write_text("changed\n", encoding="utf-8")
    result = _assert_clean(repo)

    assert result.returncode == 1
    assert "deploy/prism_ops_executor.py" in result.stderr
