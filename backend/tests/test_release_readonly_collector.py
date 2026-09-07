"""发布取证脚本本地注入验收，不执行 Docker/生产 SQL。"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "docs/完整待办与漏洞知识更新20260907/证据/collect_release_readonly.py"
    spec = importlib.util.spec_from_file_location("release_readonly_collector_test", path)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    sha = "a" * 40
    ids = {"BACKEND": "sha256:" + "b" * 64, "FRONTEND": "sha256:" + "c" * 64}
    ledger = {
        "RELEASE_SHA": sha,
        "APP_VERSION": "3.8.6",
        "BACKEND_RELEASE": sha,
        "FRONTEND_RELEASE": sha,
        "BACKEND_IMAGE_ID": ids["BACKEND"],
        "FRONTEND_IMAGE_ID": ids["FRONTEND"],
        "ALEMBIC_REVISION": collector.REVISION,
    }
    ledger_path = tmp_path / "deploy/.releases/current.env"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("\n".join(f"{k}={v}" for k, v in ledger.items()))
    containers, images = {}, {}
    for prefix in ("BACKEND", "FRONTEND"):
        name = "cr_" + prefix.lower()
        image = "prism-" + prefix.lower() + ":" + sha
        containers[name] = {
            "Id": prefix.lower() + "-container-id",
            "Image": ids[prefix],
            "Config": {"Image": image},
            "State": {"Running": True, "Health": {"Status": "healthy"}, "StartedAt": "2026-09-07T09:00:00Z"},
            "Mounts": [],
        }
        if prefix == "BACKEND":
            containers[name]["Config"]["Env"] = [
                "APP_RELEASE=" + sha,
                "APP_VERSION=3.8.6",
                "DB_PASSWORD=SECRET_SENTINEL",
            ]
        images[image] = {"Id": ids[prefix], "Config": {}}
    containers["cr_frontend"]["Mounts"] = [{"Destination": "/usr/share/nginx/html/assets"}]
    keys = [
        {
            "kind": "fk",
            "table": table,
            "column": col,
            "target_schema": "code_review",
            "target": target,
            "target_column": "id",
            "delete_rule": "SET NULL",
            "nullable": 1,
            "source_type": "bigint",
            "column_count": 1,
        }
        for table in collector.TABLES
        for col, target in collector.TARGETS.items()
    ]
    transaction = {"kind": "transaction", "schema": "code_review", "read_only": 1}
    sources = {p: ("committed:" + p).encode() for p in collector.FILES}
    hashes = {p: hashlib.sha256(raw).hexdigest() for p, raw in sources.items()}
    image_assets = {"./index.html": "1" * 64, "./assets/index-current.js": "2" * 64}
    served_assets = {**image_assets, "./assets/old-compatible.js": "3" * 64}
    calls, diffs = [], {"cr_backend": "", "cr_frontend": ""}

    def run(command, *, input=None, binary=False):
        calls.append((command, input))
        if command[0] == "git":
            if command[3] == "rev-parse":
                return sha + "\n"
            if command[-1].endswith(":VERSION"):
                return "3.8.6\n"
            require_path = command[-1].split(":backend/", 1)[1]
            assert binary
            return sources[require_path]
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([images[command[-1]]])
        if command[:2] == ["docker", "inspect"]:
            return json.dumps([containers[command[-1]]])
        if command[:2] == ["docker", "diff"]:
            return diffs[command[-1]]
        if command[:4] == ["docker", "exec", "-i", "cr_mysql"]:
            assert input.startswith("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
            assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;" in input
            assert input.endswith("ROLLBACK;")
            assert not any(word in input for word in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP "))
            return "\n".join(
                json.dumps(row) for row in [transaction, {"kind": "revision", "value": collector.REVISION}, *keys]
            )
        if command[:4] == ["docker", "exec", "-w", "/app"]:
            return json.dumps(hashes)
        if command[:3] == ["docker", "exec", "-w"]:
            selected = image_assets if command[3] == "/opt/prism-dist" else served_assets
            return "\n".join(f"{digest}  {name}" for name, digest in selected.items())
        pytest.fail("unexpected command")

    monkeypatch.setattr(collector, "run", run)
    return dict(
        collector=collector,
        sha=sha,
        containers=containers,
        images=images,
        keys=keys,
        hashes=hashes,
        sources=sources,
        image_assets=image_assets,
        served_assets=served_assets,
        transaction=transaction,
        calls=calls,
        diffs=diffs,
        ledger_path=ledger_path,
    )


def test_normal_frontend_has_no_app_env_and_real_image_assets_are_verified(snapshot):
    s = snapshot
    report = s["collector"].collect(s["sha"])
    assert report["status"] == "passed" and len(report["foreign_keys"]) == 42
    assert "Env" not in s["containers"]["cr_frontend"]["Config"]
    assert report["frontend_files"]["expected_count"] == 2 and report["frontend_files"]["served_count"] == 3
    assert len(report["running_source_sha256"]) == 5
    assert "app/services/sandbox_report_summary.py" in report["running_source_sha256"]
    assert "SECRET_SENTINEL" not in json.dumps(report)
    assert report["readonly_transaction"]["read_only"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        "tag_drift",
        "old_running_image",
        "unhealthy",
        "mount",
        "layer_change",
        "missing_fk",
        "wrong_schema",
        "not_nullable",
        "composite_fk",
        "read_write",
        "missing_source",
        "source_changed",
        "served_stale",
        "ledger_mismatch",
    ],
)
def test_contract_drift_is_rejected(snapshot, failure):
    s = snapshot
    if failure == "tag_drift":
        s["images"]["prism-frontend:" + s["sha"]]["Id"] = "sha256:" + "d" * 64
    elif failure == "old_running_image":
        s["containers"]["cr_backend"]["Image"] = "sha256:" + "d" * 64
    elif failure == "unhealthy":
        s["containers"]["cr_backend"]["State"]["Health"]["Status"] = "unhealthy"
    elif failure == "mount":
        s["containers"]["cr_backend"]["Mounts"] = [{"Destination": "/app/app"}]
    elif failure == "layer_change":
        s["diffs"]["cr_frontend"] = "C /opt/prism-dist/index.html\n"
    elif failure == "missing_fk":
        s["keys"].pop()
    elif failure == "wrong_schema":
        s["keys"][0]["target_schema"] = "different_database"
    elif failure == "not_nullable":
        s["keys"][0]["nullable"] = 0
    elif failure == "composite_fk":
        s["keys"][0]["column_count"] = 2
    elif failure == "read_write":
        s["transaction"]["read_only"] = 0
    elif failure == "missing_source":
        s["hashes"].pop(next(iter(s["hashes"])))
    elif failure == "source_changed":
        s["hashes"][next(iter(s["hashes"]))] = "0" * 64
    elif failure == "served_stale":
        s["served_assets"]["./assets/index-current.js"] = "9" * 64
    elif failure == "ledger_mismatch":
        s["ledger_path"].write_text(
            s["ledger_path"].read_text().replace("RELEASE_SHA=" + s["sha"], "RELEASE_SHA=" + "d" * 40)
        )
    with pytest.raises(s["collector"].EvidenceError):
        s["collector"].collect(s["sha"])


def test_checkout_files_cannot_replace_commit_evidence(snapshot):
    s = snapshot
    s["collector"].ROOT.joinpath("VERSION").write_text("uncommitted-version")
    report = s["collector"].collect(s["sha"])
    assert report["version"] == "3.8.6"
    assert any(command[-1] == s["sha"] + ":VERSION" for command, _input in s["calls"])


def test_main_errors_are_nonzero_and_never_include_raw_env_or_subprocess_output(snapshot, monkeypatch, capsys):
    collector = snapshot["collector"]
    monkeypatch.setattr(sys, "argv", ["collector", "--expected-release", snapshot["sha"]])

    def fail(_expected):
        raise ValueError("SECRET_SENTINEL_RAW_ENV")

    monkeypatch.setattr(collector, "collect", fail)
    assert collector.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "failed"
    assert "SECRET_SENTINEL" not in output.out + output.err
