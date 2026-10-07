from __future__ import annotations

import hashlib
import re
from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parents[1]


def test_sandbox_build_default_runner_digest_matches_source() -> None:
    runner = (DEPLOY_DIR / "sandbox" / "runner.sh").read_bytes()
    runner_digest = hashlib.sha256(runner).hexdigest()
    compose = (DEPLOY_DIR / "sandbox" / "docker-compose.build.yml").read_text(
        encoding="utf-8"
    )

    configured_digests = re.findall(
        r"PRISM_RUNNER_SHA256: \$\{PRISM_RUNNER_SHA256:-([0-9a-f]{64})\}",
        compose,
    )

    assert len(configured_digests) == 5
    assert set(configured_digests) == {runner_digest}
