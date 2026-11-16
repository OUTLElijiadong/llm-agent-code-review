"""An identity match is never permission to release the disabled foundation."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('owner_image_probe', ROOT / 'deploy/lib/assistant_owner_image_compatibility.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def copy_sources(dst):
    for path in probe.EXPECTED:
        target = dst / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'backend' / path, target)


def test_frozen_disabled_foundation_identity():
    assert probe.probe(ROOT / 'backend')


@pytest.mark.parametrize('path', list(probe.EXPECTED))
def test_altered_or_missing_module_fails_closed(tmp_path, path):
    copy_sources(tmp_path)
    target = tmp_path / path
    target.write_text(target.read_text() + '\nCOMPATIBILITY_TAMPER = True\n')
    assert not probe.probe(tmp_path)
    target.unlink()
    with pytest.raises(OSError):
        probe.probe(tmp_path)


def test_activating_foundation_changes_identity(tmp_path):
    copy_sources(tmp_path)
    target = tmp_path / 'app/services/enterprise_assistant_ownership_service.py'
    target.write_text(target.read_text().replace('_FOUNDATION_ACTIVATED = False', '_FOUNDATION_ACTIVATED = True'))
    assert not probe.probe(tmp_path)


def test_matching_identity_still_cannot_start_backend():
    cmd = '''source deploy/lib/common.sh
    docker() { echo supported; }
    assert_assistant_owner_image_compatible "sha256:$(printf '%064d' 0)"
    echo 'UNEXPECTED START'
    '''
    result = subprocess.run(['bash', '-c', cmd], cwd=ROOT, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode != 0
    assert 'UNEXPECTED START' not in result.stdout
    assert '禁止发布' in result.stderr
