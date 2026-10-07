"""agent 动态测试用例:生成注入、zip 注入、结果解析回归。"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import time
import zipfile

import pytest
from app.services.sandbox_service import (
    _extract_agent_tests_result,
    _generated_test_contract_issues,
    _inject_agent_test_files,
    _inject_deployment_patch,
    _source_summary_for_agent_tests,
)
from tests.unit.services.execution_test_rows import authorized_sandbox_environment


def _zip_with(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_source_summary_lists_files_and_entries() -> None:
    archive = _zip_with({"app/main.py": "print(1)", "tests/test_a.py": "def test_a(): pass"})
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["language"] == "python"
    assert "app/main.py" in summary["files"]
    assert "tests/test_a.py" in summary["entries"]
    assert summary["coverage_complete"] is True
    assert "app/main.py" in str(summary["source_chunks"])


def test_source_summary_packs_many_short_files_without_losing_tail() -> None:
    files = {f"src/module_{index:03d}.py": f"VALUE = {index}\n" for index in range(301)}
    archive = _zip_with(files)
    summary = _source_summary_for_agent_tests(archive, "python")
    assert len(summary["files"]) == 301
    assert "src/module_300.py" in summary["entries"]
    assert summary["coverage_complete"] is True
    assert "src/module_300.py" in str(summary["source_chunks"])


def test_source_summary_preserves_end_of_long_file_as_later_chunk() -> None:
    archive = _zip_with({"main.py": "# filler\n" * 600 + "TAIL_SECURITY_GATE = True\n"})
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["coverage_complete"] is True
    chunks = [item for item in summary["source_chunks"] if item["path"] == "main.py"]
    assert len(chunks) > 1
    assert "TAIL_SECURITY_GATE" in chunks[-1]["text"]


def test_source_summary_covers_archives_over_previous_32_chunk_ceiling() -> None:
    files = {f"module_{index}.py": f"MODULE_{index} = True\n" * 800 for index in range(40)}
    archive = _zip_with(files)
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["coverage_complete"] is True
    assert len(summary["files"]) == 40
    assert summary["source_chunk_count"] > 32
    assert "module_39.py" in str(summary["source_chunks"][-1])


def test_source_summary_rejects_ambiguous_duplicate_zip_members() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.php", "<?php echo 1;")
        zf.writestr("main.php", "<?php echo 2;")
    summary = _source_summary_for_agent_tests(base64.b64encode(buf.getvalue()).decode(), "php")
    assert summary["coverage_complete"] is False
    assert "重复路径" in summary["coverage_error"]


def test_inject_agent_test_files_adds_agent_tests_dir() -> None:
    archive = _zip_with({"main.py": "print(1)"})
    files = [{"path": "test_ai_1.py", "content": "assert 1 == 1"}]
    augmented = _inject_agent_test_files(archive, files)
    raw = base64.b64decode(augmented)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
        assert "_agent_tests/test_ai_1.py" in names
        assert zf.read("_agent_tests/test_ai_1.py").decode() == "assert 1 == 1"


def test_extract_agent_tests_result_parses_marker() -> None:
    log = (
        "start\nPRISM_AGENT_TESTS_BEGIN "
        '{"generated":2,"passed":1,"failed":1,"passed_count":1,"files":{"a.py":"pass","b.py":"fail"}} '
        "PRISM_AGENT_TESTS_END\nend"
    )
    result = _extract_agent_tests_result(log)
    assert result is not None
    assert result["generated"] == 2
    assert result["passed_count"] == 1
    assert result["files"]["b.py"] == "fail"


def test_extract_agent_tests_result_none_when_missing() -> None:
    assert _extract_agent_tests_result("no marker") is None


@pytest.mark.parametrize(
    "source",
    [
        """import os as env
from urllib.request import urlopen as open_url
from urllib.parse import urlencode
port = env.getenv('PRISM_PREVIEW_PORT')
query = urlencode({'q': 'hello world'})
response = open_url('http://127.0.0.1:' + port + '/?' + query, timeout=5)
""",
        """from os import getenv as get_port
import urllib.request as http
from urllib.parse import quote as encode
port = get_port('PRISM_PREVIEW_PORT')
query = encode('hello world')
response = http.urlopen('http://127.0.0.1:' + port + '/?q=' + query, timeout=5)
""",
        """from os import environ as env
from urllib.request import urlopen as open_url
port = env['PRISM_PREVIEW_PORT']
response = open_url(f'http://127.0.0.1:{port}/', timeout=5)
""",
        """import os
os_alias = os
port_source = os_alias.environ
port = port_source.get('PRISM_PREVIEW_PORT')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + port + '/', timeout=5)
""",
    ],
    ids=["os-module-alias", "getenv-function-alias", "environ-module-alias", "assignment-aliases"],
)
def test_blackbox_contract_accepts_safe_import_aliases_for_dynamic_port(source: str) -> None:
    assert _generated_test_contract_issues([{"path": "blackbox.py", "content": source}], "python") == []


@pytest.mark.parametrize(
    "source",
    [
        """from urllib.request import urlopen
urlopen('http://127.0.0.1:8080/', timeout=5)
""",
        """from urllib.request import urlopen
urlopen('http://example.test:' + __import__('os').getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """from urllib.request import urlopen
urlopen('http://127.0.0.1:' + __import__('os').getenv('PRISM_PREVIEW_PORT') + '/?q=hello world', timeout=5)
""",
    ],
    ids=["fixed-port", "non-loopback-host", "raw-payload"],
)
def test_blackbox_contract_still_rejects_unsafe_targets_and_raw_payloads(source: str) -> None:
    issues = _generated_test_contract_issues([{"path": "blackbox.py", "content": source}], "python")
    assert issues


@pytest.mark.parametrize(
    "source",
    [
        """import os
os.environ = {'PRISM_PREVIEW_PORT': '8080'}
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os as env
env.environ = {'PRISM_PREVIEW_PORT': '8080'}
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + env.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
os.getenv = lambda _key: '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """from os import getenv as get_port
get_port = lambda _key: '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + get_port('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environ.update({'PRISM_PREVIEW_PORT': '8080'})
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """from os import environ as env
env['PRISM_PREVIEW_PORT'] = '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + env['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
setattr(os, 'getenv', lambda _key: '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environ |= {'PRISM_PREVIEW_PORT': '8080'}
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
dict.__setitem__(os.environ, 'PRISM_PREVIEW_PORT', '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
os.__dict__['getenv'] = lambda _key: '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
osdict = os.__dict__
osdict['environ'] = {'PRISM_PREVIEW_PORT': '8080'}
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
os_alias = os
os_alias.__dict__['getenv'] = lambda _key: '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os_alias.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
env_data = os.environ._data
env_data['PRISM_PREVIEW_PORT'] = '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.environ['PRISM_PREVIEW_PORT'] + '/', timeout=5)
""",
        """import os
vars(os)['getenv'] = lambda _key: '8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
operator.setitem(os.environ, 'PRISM_PREVIEW_PORT', '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
type(os.environ).__setitem__(os.environ, 'PRISM_PREVIEW_PORT', '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environ._data.update({b'PRISM_PREVIEW_PORT': b'8080'})
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
env_data = os.environ._data
operator.setitem(env_data, b'PRISM_PREVIEW_PORT', b'8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
operator.setitem(os.__dict__, 'getenv', lambda _key: '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
operator.delitem(os.environ, 'PRISM_PREVIEW_PORT')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT', '8080') + '/', timeout=5)
""",
        """import os
from operator import delitem as drop_item
drop_item(os.environ, 'PRISM_PREVIEW_PORT')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT', '8080') + '/', timeout=5)
""",
        """import os
import operator
mutate = operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')
mutate(os.environ)
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
from operator import methodcaller as make_mutator
mutate = make_mutator('__setitem__', 'PRISM_PREVIEW_PORT', '8080')
mutate(os.environ)
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
type(os).__setattr__(os, 'getenv', lambda _key: '8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environb[b'PRISM_PREVIEW_PORT'] = b'8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environb.update({b'PRISM_PREVIEW_PORT': b'8080'})
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
env_data = os.environb._data
env_data[b'PRISM_PREVIEW_PORT'] = b'8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """from os import environb as envb
envb[b'PRISM_PREVIEW_PORT'] = b'8080'
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + __import__('os').getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
env_alias = os.environb
env_alias.update({b'PRISM_PREVIEW_PORT': b'8080'})
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
operator.setitem(os.environb, b'PRISM_PREVIEW_PORT', b'8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
import operator
operator.methodcaller('__setitem__', b'PRISM_PREVIEW_PORT', b'8080')(os.environb)
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
os.environb |= {b'PRISM_PREVIEW_PORT': b'8080'}
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
        """import os
type(os.environb).__setitem__(os.environb, b'PRISM_PREVIEW_PORT', b'8080')
from urllib.request import urlopen
urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)
""",
    ],
    ids=[
        "replace-environ",
        "replace-environ-module-alias",
        "replace-getenv",
        "replace-getenv-import-alias",
        "update-environ",
        "write-environ-import-alias",
        "setattr-getenv",
        "augassign-environ",
        "dict-setitem-environ",
        "os-module-dict-getenv",
        "os-module-dict-environ-alias",
        "os-module-alias-dict-getenv",
        "environ-internal-data-alias",
        "vars-module-dict-getenv",
        "operator-setitem-environ",
        "environ-type-setitem",
        "environ-private-data-update",
        "operator-setitem-environ-data-alias",
        "operator-setitem-module-dict",
        "module-type-setattr",
        "operator-delitem-environ",
        "operator-delitem-alias-environ",
        "operator-methodcaller-setitem-environ",
        "operator-methodcaller-import-alias-environ",
        "replace-dynamic-port-through-environb",
        "update-dynamic-port-through-environb",
        "mutate-environb-private-data",
        "write-environb-import-alias",
        "update-environb-assignment-alias",
        "operator-setitem-environb",
        "operator-methodcaller-environb",
        "augassign-environb",
        "environb-type-setitem",
    ],
)
def test_blackbox_contract_rejects_mutated_dynamic_port_sources(source: str) -> None:
    issues = _generated_test_contract_issues([{"path": "blackbox.py", "content": source}], "python")
    assert issues


@pytest.mark.parametrize(
    ("body", "rejected"),
    [
        (
            "from operator import methodcaller\n"
            "methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator as op\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "methodcaller_factory = operator.methodcaller\n"
            "methodcaller_factory('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator as op\n"
            "methodcaller_factory = op.methodcaller\n"
            "methodcaller_alias = methodcaller_factory\n"
            "methodcaller_alias('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "op = operator\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "first_alias = operator\n"
            "second_alias = first_alias\n"
            "second_alias.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import _operator as op\n"
            "alias = op\n"
            "alias.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "(module_alias,) = (operator,)\n"
            "module_alias.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "(factory,) = (operator.methodcaller,)\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "from operator import methodcaller as factory\n"
            "(first,) = (factory,)\n"
            "(second,) = (first,)\n"
            "second('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "getattr(operator, 'methodcaller')('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "def make_factory():\n"
            "    return operator.methodcaller\n"
            "factory = make_factory()\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "from operator import methodcaller as make_call\n"
            "result = make_call('upper')('safe')\n",
            False,
        ),
        (
            "import json\n"
            "import operator\n"
            "from urllib.request import urlopen\n"
            "json.dumps(os.environ)\n"
            "operator.methodcaller('upper')('safe')\n",
            False,
        ),
        (
            "from urllib.request import urlopen\n"
            "import operator\n"
            "def read_port(mapping):\n"
            "    return mapping.get('PRISM_PREVIEW_PORT')\n"
            "port = read_port(os.environ)\n"
            "operator.methodcaller('upper')('safe')\n",
            False,
        ),
        (
            "getattr(__import__('operator'), 'methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "__import__('operator').__dict__['methodcaller']"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import builtins\n"
            "builtins.__import__('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "from builtins import __import__ as load_module\n"
            "load_module('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "__builtins__['__import__']('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import importlib\n"
            "importlib.import_module('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "from importlib import import_module as load_module\n"
            "load_module('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8080')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')"
            "(os.__dict__['environ'])\n",
            True,
        ),
        (
            "import operator\n"
            "operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8080')"
            "(os.__dict__.get('environ'))\n",
            True,
        ),
        (
            "import operator\n"
            "operator.methodcaller('__setitem__', 'OTHER_FLAG', '1')(os.environ)\n",
            False,
        ),
        (
            "os.environ['OTHER_FLAG'] = '1'\n",
            False,
        ),
        (
            "import operator\n"
            "operator.__dict__.get('methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8101')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "vars(operator).get('methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8102')(os.environ)\n",
            True,
        ),
        (
            "import sys\n"
            "sys.modules.get('operator').methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8103')(os.environ)\n",
            True,
        ),
        (
            "import functools\n"
            "import operator\n"
            "functools.partial(operator.methodcaller"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8104'), os.environ)()\n",
            True,
        ),
        (
            "import functools\n"
            "import operator\n"
            "mutate = functools.partial(operator.methodcaller,"
            " '__setitem__', 'PRISM_PREVIEW_PORT', '8105')\n"
            "mutate()(os.environ)\n",
            True,
        ),
        (
            "import sys\n"
            "op = sys.modules.get('operator')\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8106')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "get_method = operator.__dict__.get\n"
            "get_method('methodcaller')('__setitem__', 'PRISM_PREVIEW_PORT', '8107')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "get_method = vars(operator).get\n"
            "get_method('methodcaller')('__setitem__', 'PRISM_PREVIEW_PORT', '8108')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "getattr(vars(operator), 'methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8109')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "getattr(operator.__dict__, 'get')('methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8110')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "from builtins import getattr as attr\n"
            "attr(operator, 'methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8111')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "dict.__getitem__(operator.__dict__, 'methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8112')(os.environ)\n",
            True,
        ),
        (
            "import importlib\n"
            "op = importlib.import_module('operator')\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8113')(os.environ)\n",
            True,
        ),
        (
            "op = __import__('operator')\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8114')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "fetch = dict.__getitem__\n"
            "fetch(operator.__dict__, 'methodcaller')"
            "('__setitem__', 'PRISM_PREVIEW_PORT', '8202')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "def mutate(target):\n"
            "    operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8201')(target)\n"
            "mutate(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "def apply(fn, target):\n"
            "    fn(target)\n"
            "apply(operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8203'), os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "mutate = lambda target: operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8204')(target)\n"
            "mutate(os.environ)\n",
            True,
        ),
        (
            "import importlib\n"
            "def load(name):\n"
            "    return importlib.import_module(name)\n"
            "op = load('operator')\n"
            "op.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8204')(os.environ)\n",
            True,
        ),
        (
            "import functools\n"
            "import operator\n"
            "class Mutator:\n"
            "    def change(self, target):\n"
            "        operator.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8206')(target)\n"
            "    run = functools.partialmethod(change)\n"
            "Mutator().run(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "class Resolver:\n"
            "    @property\n"
            "    def factory(self):\n"
            "        return operator.methodcaller\n"
            "Resolver().factory('__setitem__', 'PRISM_PREVIEW_PORT', '8203')(os.environ)\n",
            True,
        ),
        (
            "class FactoryDescriptor:\n"
            "    def __get__(self, instance, owner):\n"
            "        return __import__('operator').methodcaller\n"
            "class Resolver:\n"
            "    factory = FactoryDescriptor()\n"
            "Resolver().factory('__setitem__', 'PRISM_PREVIEW_PORT', '8204')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "container = (operator.methodcaller,)\n"
            "factory = container[0]\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8206')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "container = {'factory': operator.methodcaller}\n"
            "factory = container['factory']\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8207')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "namespace = vars(operator)\n"
            "factory = namespace['methodcaller']\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8208')(os.environ)\n",
            True,
        ),
        (
            "class Dispatcher:\n"
            "    def __init__(self): self.target = __import__('operator').methodcaller\n"
            "    def __call__(self, *args): return self.target(*args)\n"
            "Dispatcher()('__setitem__', 'PRISM_PREVIEW_PORT', '8210')(os.environ)\n",
            True,
        ),
        (
            "import importlib\n"
            "module = importlib.import_module(''.join(['oper', 'ator']))\n"
            "factory = getattr(module, ''.join(['method', 'caller']))\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8212')(os.environ)\n",
            True,
        ),
        (
            "loader = getattr(globals()['__builtins__'], '__import__')\n"
            "loader('operator').methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8213')(os.environ)\n",
            True,
        ),
        (
            "import operator\n"
            "def expose(fn): return fn\n"
            "factory = expose(operator.methodcaller)\n"
            "factory('__setitem__', 'PRISM_PREVIEW_PORT', '8217')(os.environ)\n",
            True,
        ),
    ],
    ids=[
        "nested-name-mutator",
        "nested-attribute-mutator",
        "nested-assigned-factory",
        "nested-assigned-factory-chain",
        "operator-module-alias",
        "operator-module-alias-chain",
        "underscore-operator-module-alias",
        "operator-module-destructuring-alias",
        "methodcaller-destructuring-alias",
        "methodcaller-destructuring-function-chain",
        "getattr-methodcaller",
        "factory-returning-methodcaller",
        "nested-benign-call",
        "benign-methodcaller-with-json-environ",
        "benign-methodcaller-with-read-only-environ-helper",
        "getattr-import-reflection-mutator",
        "module-dict-reflection-mutator",
        "builtins-import-reflection-mutator",
        "import-alias-reflection-mutator",
        "builtins-dict-import-reflection-mutator",
        "importlib-import-module-reflection-mutator",
        "importlib-function-reflection-mutator",
        "os-module-dict-environ-reflection-mutator",
        "os-module-dict-get-environ-reflection-mutator",
        "mutator-changes-unrelated-environment-key",
        "direct-change-to-unrelated-environment-key",
        "operator-dict-get-methodcaller-mutator",
        "operator-vars-get-methodcaller-mutator",
        "sys-modules-get-methodcaller-mutator",
        "functools-partial-binds-environ",
        "functools-partial-methodcaller-factory",
        "assigned-sys-modules-operator-alias",
        "assigned-operator-dict-get-alias",
        "assigned-operator-vars-get-alias",
        "getattr-operator-vars-dict",
        "getattr-operator-dict-get-alias",
        "builtins-getattr-import-alias",
        "dict-getitem-operator-dict",
        "assigned-importlib-operator-alias",
        "assigned-builtin-import-operator-alias",
        "assigned-dict-getitem-methodcaller",
        "function-parameter-environ-mutator",
        "function-parameter-methodcaller-callback",
        "lambda-parameter-environ-mutator",
        "function-return-imported-operator-alias",
        "partialmethod-environ-mutator",
        "property-methodcaller-factory",
        "descriptor-methodcaller-factory",
        "tuple-index-methodcaller-factory",
        "dictionary-index-methodcaller-factory",
        "vars-dictionary-methodcaller-factory",
        "callable-wrapper-methodcaller-mutator",
        "joined-dynamic-import-methodcaller-mutator",
        "globals-builtin-import-methodcaller-mutator",
        "helper-returned-methodcaller-mutator",
    ],
)
def test_nested_call_ast_contract_does_not_crash_and_keeps_environment_guard(body: str, rejected: bool) -> None:
    source = "import os\n" + body
    source = source.replace("import os\n", "import os\nfrom urllib.request import urlopen\n")
    source = source.rstrip() + "\nurlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT') + '/', timeout=5)\n"
    issues = _generated_test_contract_issues([{"path": "blackbox.py", "content": source}], "python")

    if rejected:
        assert any("PRISM_PREVIEW_PORT" in issue for issue in issues)
    else:
        assert issues == []


def test_blackbox_contract_accepts_encoded_url_with_read_only_port_helper() -> None:
    source = """import os
from urllib.parse import urlencode
from urllib.request import urlopen

def read_port(mapping):
    return mapping.get('PRISM_PREVIEW_PORT')

port = read_port(os.environ)
query = urlencode({'q': 'hello world'})
urlopen('http://127.0.0.1:' + port + '/?' + query, timeout=5)
"""
    assert _generated_test_contract_issues([{"path": "blackbox.py", "content": source}], "python") == []


def _blackbox_contract_source(body: str) -> str:
    return (
        "import os\n"
        "from urllib.parse import urlencode\n"
        "from urllib.request import urlopen\n"
        f"{body.rstrip()}\n"
        "query = urlencode({'q': 'safe query'})\n"
        "urlopen('http://127.0.0.1:' + os.getenv('PRISM_PREVIEW_PORT', '8099') + '/?' + query, timeout=5)\n"
    )


def _isolated_preview_port_after(body: str, initial_port: str = "8200") -> str:
    completed = subprocess.run(
        [sys.executable, "-c", f"import os\n{body.rstrip()}\nprint(os.getenv('PRISM_PREVIEW_PORT', '<missing>'))\n"],
        env={**os.environ, "PRISM_PREVIEW_PORT": initial_port},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize(
    ("body", "expected_port"),
    [
        ("exec(\"os.environ['PRISM_PREVIEW_PORT']='8701'\")", "8701"),
        ("run = exec\nrun(\"os.environ['PRISM_PREVIEW_PORT']='8702'\")", "8702"),
        ("eval(\"os.environ.__setitem__('PRISM_PREVIEW_PORT','8703')\")", "8703"),
        ("evaluate = eval\nevaluate(\"os.environ.__setitem__('PRISM_PREVIEW_PORT','8704')\")", "8704"),
        (
            "import builtins\nbuiltins.exec(\"os.environ['PRISM_PREVIEW_PORT']='8705'\")",
            "8705",
        ),
        (
            "from builtins import exec as run\nrun(\"os.environ['PRISM_PREVIEW_PORT']='8706'\")",
            "8706",
        ),
        (
            "code = compile(\"os.environ['PRISM_PREVIEW_PORT']='8707'\", '<generated>', 'exec')\nexec(code)",
            "8707",
        ),
        (
            "def mutate(source):\n    exec(source)\nmutate(\"os.environ['PRISM_PREVIEW_PORT']='8708'\")",
            "8708",
        ),
        (
            "import builtins\nrun = getattr(builtins, 'ex' + 'ec')\nrun(\"os.environ['PRISM_PREVIEW_PORT']='8709'\")",
            "8709",
        ),
        (
            "import builtins\nrun = builtins.__dict__['ex' + 'ec']\n"
            "run(\"os.environ['PRISM_PREVIEW_PORT']='8710'\")",
            "8710",
        ),
        (
            "import builtins\nnamespace = vars(builtins)\nrun = namespace['ex' + 'ec']\n"
            "run(\"os.environ['PRISM_PREVIEW_PORT']='8711'\")",
            "8711",
        ),
        (
            "run = __import__('builtins').__dict__['ex' + 'ec']\n"
            "run(\"os.environ['PRISM_PREVIEW_PORT']='8712'\")",
            "8712",
        ),
    ],
    ids=[
        "exec-direct",
        "exec-alias",
        "eval-direct",
        "eval-alias",
        "builtins-exec",
        "builtins-exec-import-alias",
        "compile-then-exec",
        "helper-inner-exec",
        "builtins-reflective-exec",
        "builtins-dict-dynamic-key",
        "vars-builtins-dynamic-key",
        "import-builtin-dict-dynamic-key",
    ],
)
def test_blackbox_contract_rejects_dynamic_python_execution_aliases(body: str, expected_port: str) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert any("exec/eval/compile" in issue for issue in issues)
    assert _isolated_preview_port_after(body) == expected_port


@pytest.mark.parametrize(
    ("body", "expected_port"),
    [
        (
            "def mutate(*args):\n    args[0]['PRISM_PREVIEW_PORT'] = '8801'\nmutate(os.environ)",
            "8801",
        ),
        (
            "def mutate(**kwargs):\n    kwargs['mapping']['PRISM_PREVIEW_PORT'] = '8802'\nmutate(mapping=os.environ)",
            "8802",
        ),
        (
            "def mutate(**kwargs):\n    kwargs['env']['PRISM_PREVIEW_PORT'] = '8804'\n"
            "kwargs = {'env': os.environ}\nmutate(**kwargs)",
            "8804",
        ),
    ],
    ids=["varargs-environment-source", "kwargs-environment-source", "kwargs-expanded-dict-environment-source"],
)
def test_blackbox_contract_rejects_varargs_and_kwargs_environment_mutation(body: str, expected_port: str) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert any("PRISM_PREVIEW_PORT" in issue for issue in issues)
    assert _isolated_preview_port_after(body) == expected_port


@pytest.mark.parametrize(
    ("body", "expected_port"),
    [
        (
            "setter = getattr(os.environ, '__setitem__')\n"
            "setter('PRISM_PREVIEW_PORT', '8817')",
            "8817",
        ),
        (
            "data = os.environ._data\nsetter = getattr(dict, '__setitem__')\n"
            "setter(data, b'PRISM_PREVIEW_PORT', b'8814')",
            "8814",
        ),
        (
            "data = os.environ._data\nsetter = type(data).__setitem__\n"
            "setter(data, b'PRISM_PREVIEW_PORT', b'8815')",
            "8815",
        ),
        (
            "data = os.environ._data\nupdate = getattr(dict, 'update')\n"
            "update(data, {b'PRISM_PREVIEW_PORT': b'8816'})",
            "8816",
        ),
        (
            "data = os.environ._data\nsetter = dict.__dict__['__setitem__']\n"
            "setter(data, b'PRISM_PREVIEW_PORT', b'8818')",
            "8818",
        ),
        (
            "setter = os.environ.__setitem__\nsetter('PRISM_PREVIEW_PORT', '8819')",
            "8819",
        ),
        (
            "updater = os.environ.update\nupdater({'PRISM_PREVIEW_PORT': '8820'})",
            "8820",
        ),
        (
            "from functools import partial\n"
            "setter = partial(os.environ.__setitem__, 'PRISM_PREVIEW_PORT', '8821')\nsetter()",
            "8821",
        ),
        (
            "from functools import partial\n"
            "setter = partial(dict.__setitem__, os.environ._data, b'PRISM_PREVIEW_PORT', b'8822')\nsetter()",
            "8822",
        ),
        (
            "import operator\nsetter = getattr(operator, 'setitem')\n"
            "setter(os.environ, 'PRISM_PREVIEW_PORT', '8823')",
            "8823",
        ),
        (
            "setter = object.__getattribute__(os.environ, '__setitem__')\n"
            "setter('PRISM_PREVIEW_PORT', '8830')",
            "8830",
        ),
        (
            "setter = os.environ.__getattribute__('__setitem__')\n"
            "setter('PRISM_PREVIEW_PORT', '8831')",
            "8831",
        ),
        (
            "fetch = object.__getattribute__\nsetter = fetch(os.environ, '__setitem__')\n"
            "setter('PRISM_PREVIEW_PORT', '8910')",
            "8910",
        ),
        (
            "fetch = os.environ.__getattribute__\nsetter = fetch('__setitem__')\n"
            "setter('PRISM_PREVIEW_PORT', '8911')",
            "8911",
        ),
        (
            "def get_setter(mapping):\n    return mapping.__setitem__\n"
            "setter = get_setter(os.environ)\nsetter('PRISM_PREVIEW_PORT', '8913')",
            "8913",
        ),
    ],
    ids=[
        "environ-bound-setitem-alias",
        "dict-bound-setitem-alias",
        "type-bound-setitem-alias",
        "dict-bound-update-alias",
        "dict-descriptor-setitem-alias",
        "environ-direct-method-alias",
        "environ-bound-update-alias",
        "partial-bound-environ-method",
        "partial-unbound-dict-method",
        "operator-reflective-setitem-alias",
        "object-getattribute-bound-setitem",
        "environ-getattribute-bound-setitem",
        "object-getattribute-function-alias",
        "bound-getattribute-function-alias",
        "helper-returned-environment-mutator",
    ],
)
def test_blackbox_contract_rejects_reflective_environment_mutator_aliases(body: str, expected_port: str) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert any("PRISM_PREVIEW_PORT" in issue for issue in issues)
    assert _isolated_preview_port_after(body) == expected_port


@pytest.mark.parametrize(
    "body",
    [
        "setter = os.environ.__setitem__\nsetter('OTHER_FLAG', 'safe')",
        "updater = os.environ.update\nupdater({'OTHER_FLAG': 'safe'})",
    ],
    ids=["unrelated-bound-setitem", "unrelated-bound-update"],
)
def test_blackbox_contract_accepts_environment_mutations_unrelated_to_preview_port(body: str) -> None:
    assert _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    ) == []


@pytest.mark.parametrize(
    "body",
    [
        "def read_port(*args):\n    return args[0].get('PRISM_PREVIEW_PORT')\nport = read_port(os.environ)",
        (
            "def read_port(**kwargs):\n    return kwargs['mapping'].get('PRISM_PREVIEW_PORT')\n"
            "port = read_port(mapping=os.environ)"
        ),
        (
            "def read_port(**kwargs):\n    return kwargs['mapping'].get('PRISM_PREVIEW_PORT')\n"
            "source = {'mapping': os.environ}\nport = read_port(**source)"
        ),
    ],
    ids=["read-only-varargs-helper", "read-only-kwargs-helper", "read-only-expanded-kwargs-helper"],
)
def test_blackbox_contract_accepts_read_only_varargs_and_kwargs_port_helpers(body: str) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert issues == []


@pytest.mark.parametrize(
    "body",
    [
        "get_port = os.environ.get\nport = get_port('PRISM_PREVIEW_PORT')",
        "get_port = getattr(os.environ, 'get')\nport = get_port('PRISM_PREVIEW_PORT')",
        "port = getattr(os.environ, 'get')('PRISM_PREVIEW_PORT')",
        "get_port = object.__getattribute__(os.environ, 'get')\nport = get_port('PRISM_PREVIEW_PORT')",
    ],
    ids=[
        "bound-environ-get-alias",
        "reflective-environ-get-alias",
        "inline-reflective-environ-get",
        "object-getattribute-environ-get-alias",
    ],
)
def test_blackbox_contract_accepts_read_only_bound_environment_getters(body: str) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert issues == []


@pytest.mark.parametrize(
    ("body", "expected_port"),
    [
        (
            "data = os.environ._data\nset_item = dict.__setitem__\nset_item(data, b'PRISM_PREVIEW_PORT', b'8811')",
            "8811",
        ),
        (
            "data = os.environ._data\nupdate = dict.update\nupdate(data, {b'PRISM_PREVIEW_PORT': b'8812'})",
            "8812",
        ),
        (
            "data = os.environ._data\ndel_item = dict.__delitem__\ndel_item(data, b'PRISM_PREVIEW_PORT')",
            "<missing>",
        ),
        (
            "data = os.environ._data\npop_item = dict.pop\npop_item(data, b'PRISM_PREVIEW_PORT', None)",
            "<missing>",
        ),
    ],
    ids=["dict-setitem-alias", "dict-update-alias", "dict-delitem-alias", "dict-pop-alias"],
)
def test_blackbox_contract_rejects_dict_descriptor_aliases_mutating_environ(
    body: str, expected_port: str
) -> None:
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert any("PRISM_PREVIEW_PORT" in issue for issue in issues)
    assert _isolated_preview_port_after(body) == expected_port


def test_blackbox_contract_allows_dict_descriptor_alias_for_unrelated_environ_key() -> None:
    body = "data = os.environ._data\nset_item = dict.__setitem__\nset_item(data, b'OTHER_FLAG', b'1')"
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source(body)}], "python"
    )
    assert issues == []
    assert _isolated_preview_port_after(body) == "8200"


def test_blackbox_contract_bounds_static_string_products_and_fails_closed() -> None:
    letters = list("operator")
    parameter_count = 13
    body_lines: list[str] = []
    fragments: list[str] = []
    for index in range(parameter_count):
        function_name = f"fragment_{index}"
        included = letters[index] if index < len(letters) else "x"
        runtime_include = index < len(letters)
        body_lines.extend(
            [
                f"def {function_name}():",
                f"    if {runtime_include!r}:",
                f"        return {included!r}",
                "    return ''",
            ]
        )
        fragments.append(f"{function_name}()")
    body_lines.extend(
        [
            "module_name = " + repr("{}" * parameter_count) + ".format(" + ", ".join(fragments) + ")",
            "operator_module = __import__(module_name)",
            "operator_module.methodcaller('__setitem__', 'PRISM_PREVIEW_PORT', '8899')(os.environ)",
        ]
    )
    started = time.monotonic()
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": _blackbox_contract_source("\n".join(body_lines))}],
        "python",
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert any("PRISM_PREVIEW_PORT" in issue for issue in issues)


def test_blackbox_contract_keeps_encoded_high_complexity_query_with_trusted_port() -> None:
    lines = [
        "import os",
        "from urllib.parse import urlencode",
        "from urllib.request import urlopen",
    ]
    arguments = []
    for index in range(11):
        name = f"piece_{index}"
        choice = "x" if index % 2 == 0 else "y"
        lines.extend(
            [
                f"def {name}():",
                "    if True:",
                f"        return {choice!r}",
                "    return ''",
            ]
        )
        arguments.append(f"{name}()")
    lines.extend(
        [
            "value=" + repr("{}" * 11) + ".format(" + ",".join(arguments) + ")",
            "query=urlencode({'q':value})",
            "port=os.environ.get('PRISM_PREVIEW_PORT')",
            "urlopen('http://127.0.0.1:'+port+'/?'+query,timeout=5)",
        ]
    )
    issues = _generated_test_contract_issues(
        [{"path": "blackbox.py", "content": "\n".join(lines)}], "python"
    )
    assert issues == []


def test_inject_deployment_patch_adds_launch_script() -> None:
    archive = _zip_with({"main.py": "print(1)"})
    augmented = _inject_deployment_patch(archive, "exec python main.py\n")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(augmented))) as zf:
        assert "_prism_launch.sh" in zf.namelist()
        assert zf.read("_prism_launch.sh").decode() == "exec python main.py\n"


def test_syntax_repair_round_writes_complete_reconstructed_file(db, monkeypatch) -> None:

    from app.services import sandbox_service

    source = "<?php\n" + "// keep\n" * 6_000 + "echo broken;\n"
    archive = _zip_with(
        {
            "main.php": source,
            "untouched.php": "<?php echo 'keep';\n",
            "_agent_tests/test_generated.php": "<?php echo 'runner-only';\n",
        }
    )
    environment = authorized_sandbox_environment(
        db,
        public_id="sbx_repair",
        owner_id=7,
        project_id=9,
        language="php",
        source_sha256="parent-sha",
    )

    class FakeRepair:
        _api_key = "configured"

        def repair(self, **_kwargs):
            return {"files": {"main.php": source.replace("echo broken;", "echo 'fixed';")}}

    monkeypatch.setattr(sandbox_service, "SyntaxRepairAgent", FakeRepair)
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda _db, agent, _user_id: agent)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    result = sandbox_service._syntax_repair_round(
        db,
        environment,
        archive,
        [{"file": "main.php", "line": 6002, "message": "unexpected identifier"}],
    )
    assert result is not None
    revision = result["repair_revision"]
    assert revision["revision_id"] > 0
    assert revision["revision_no"] == 1
    assert revision["parent_source_sha256"] == "parent-sha"
    assert revision["revision_source_sha256"] != revision["execution_source_sha256"]
    assert hashlib.sha256(base64.b64decode(result["source"])).hexdigest() == revision["execution_source_sha256"]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["source"]))) as zf:
        assert zf.read("main.php").decode() == source.replace("echo broken;", "echo 'fixed';")
        assert zf.read("untouched.php").decode() == "<?php echo 'keep';\n"
        assert "_agent_tests/test_generated.php" in zf.namelist()
        assert zf.namelist().count("main.php") == 1


def test_syntax_repair_revision_config_binds_repair_to_next_worker_request() -> None:
    import json

    from app.services.sandbox_service import _append_repair_revision_to_config

    revision = {
        "revision_id": 31,
        "revision_no": 4,
        "parent_source_sha256": "parent",
        "revision_source_sha256": "saved-revision",
        "execution_source_sha256": "worker-archive",
        "repaired_files": ["main.php"],
    }
    config = json.loads(
        _append_repair_revision_to_config(
            '{"source_revision_id": 12,"syntax_repair_revisions":[]}',
            revision=revision,
            repair_round=1,
            worker_request_id="sbx_repair-r1",
        )
    )
    assert config["source_revision_id"] == 12
    assert config["syntax_repair_revisions"] == [{**revision, "repair_round": 1, "worker_request_id": "sbx_repair-r1"}]


def test_worker_receipt_requires_exact_request_and_archive_digest() -> None:
    import pytest
    from app.services.sandbox_service import _validate_worker_execution_receipt

    valid = {"request_id": "sbx_1-r1", "source_sha256": "a" * 64}
    assert _validate_worker_execution_receipt(valid, request_id="sbx_1-r1", source_sha256="a" * 64) == valid
    with pytest.raises(RuntimeError, match="request_id"):
        _validate_worker_execution_receipt(valid, request_id="sbx_1-r2", source_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="SHA-256"):
        _validate_worker_execution_receipt(
            {"request_id": "sbx_1-r1", "source_sha256": "b" * 64},
            request_id="sbx_1-r1",
            source_sha256="a" * 64,
        )


def test_large_source_does_not_call_dynamic_test_agent_with_partial_context(db, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import sandbox_service

    calls: list[dict] = []

    class FakeGenerator:
        _api_key = "configured"

        def generate(self, **kwargs):
            calls.append(kwargs)
            return {"files": []}

    archive = _zip_with({f"module_{index}.py": "x = 1\n" * 800 for index in range(40)})
    environment = SimpleNamespace(
        public_id="sbx_large",
        owner_id=7,
        project_id=9,
        agent_config_json="{}",
    )
    monkeypatch.setattr(
        "app.agents.test_case_generator_agent.TestCaseGeneratorAgent",
        FakeGenerator,
    )
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda _db, agent, _user_id: agent)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    result = sandbox_service._generate_agent_test_cases(db, environment, archive, "python", "whitebox")
    assert result is None
    assert calls == []


def test_generate_deployment_patch_uses_agent_plan(db, monkeypatch) -> None:

    from app.services.sandbox_service import _generate_deployment_patch

    environment = authorized_sandbox_environment(
        db,
        id=1,
        public_id="sbx_deploy_patch",
        project_id=1,
        owner_id=1,
        test_mode="blackbox",
        language="python",
    )
    captured: dict = {}
    monkeypatch.setattr("app.services.sandbox_service.configure_subagent", lambda _db, agent, user_id: agent)

    class FakeDeploymentAgent:
        _api_key = "test-key"

        def plan(self, **kwargs):
            captured.update(kwargs)
            return {"launch_script": "exec python -m http.server $PRISM_PREVIEW_PORT", "notes": "无入口,已补全"}

    monkeypatch.setattr(
        "app.agents.deployment_coordinator_agent.DeploymentCoordinatorAgent",
        FakeDeploymentAgent,
    )
    result = _generate_deployment_patch(db, environment, _zip_with({"a.py": "x=1"}), "python")
    assert result is not None
    assert "launch_script" in result
    assert captured["language"] == "python"


def test_deployment_coordinator_shares_deadline_with_final_model_call(monkeypatch) -> None:
    import time
    from types import SimpleNamespace

    from app.agents import deployment_coordinator_agent as module
    from app.agents.deployment_coordinator_agent import DeploymentCoordinatorAgent

    deadline = time.monotonic() + 60
    captured = {}
    monkeypatch.setattr(
        module,
        "compact_source_context",
        lambda *_args, **_kwargs: {
            "covered_source_ids": ["source-a"],
            "source_summaries": [{"source_id": "all-source-summaries", "summary": "入口已检查。"}],
        },
    )

    class Agent(DeploymentCoordinatorAgent):
        def __init__(self):
            self._api_key = "configured"

        def call_json(self, _message, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(success=True, data={"launch_script": "", "notes": ""})

    result = Agent().plan(
        language="python",
        test_mode="whitebox",
        source_summary={"coverage_complete": True, "source_chunks": []},
        deadline=deadline,
    )

    assert result["launch_script"] == ""
    assert captured["deadline_monotonic"] == deadline
