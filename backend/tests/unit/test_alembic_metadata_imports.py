"""在无应用启动和无数据库连接的冷进程中核验 Alembic 模型闭包。"""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import json
import sys
from pathlib import Path
from pydantic_settings import BaseSettings

original_init = BaseSettings.__init__
def init_without_dotenv(instance, *args, **kwargs):
    kwargs['_env_file'] = None
    original_init(instance, *args, **kwargs)
BaseSettings.__init__ = init_without_dotenv

def forbid_external_io(event, arguments):
    if event == 'open' and isinstance(arguments[0], (str, bytes)):
        name = Path(arguments[0]).name
        if name == '.env' or name.startswith('.env.'):
            raise AssertionError('dotenv read is forbidden')
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('network access is forbidden')
sys.addaudithook(forbid_external_io)

from sqlalchemy.engine import Engine
original_connect = Engine.connect
def forbid_connect(*args, **kwargs):
    raise AssertionError('database connection is forbidden')
Engine.connect = forbid_connect
"""


def cold_probe(source):
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(BOOTSTRAP) + textwrap.dedent(source)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def declared_tables():
    tables = set()
    for source in (BACKEND / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__tablename__" for target in node.targets
            ):
                tables.add(ast.literal_eval(node.value))
    return tables


def test_alembic_env_imports_every_declared_table_without_application_startup():
    result = cold_probe("""
        import runpy
        from contextlib import nullcontext
        from unittest.mock import patch
        from alembic.config import Config
        with patch('alembic.context.config', Config(), create=True), \\
             patch('alembic.context.is_offline_mode', return_value=True), \\
             patch('alembic.context.configure'), \\
             patch('alembic.context.begin_transaction', return_value=nullcontext()), \\
             patch('alembic.context.run_migrations'):
            namespace = runpy.run_path('alembic/env.py')
        imported = sorted(name for name in sys.modules if name.startswith(('app.services', 'app.agents', 'app.main')))
        print(json.dumps({'tables': sorted(namespace['target_metadata'].tables), 'side_effect_imports': imported}))
    """)

    assert set(result["tables"]) == declared_tables()
    assert result["side_effect_imports"] == []


def test_model_registry_is_explicit_lazy_and_idempotent():
    result = cold_probe("""
        import app.models
        lazy = not any(name.startswith('app.core.') for name in sys.modules)
        app.models.load_all_models()
        from app.core.database import Base
        before = dict(Base.metadata.tables)
        app.models.load_all_models()
        print(json.dumps({
            'lazy': lazy,
            'tables': sorted(Base.metadata.tables),
            'stable': all(Base.metadata.tables[name] is table for name, table in before.items()),
            'side_effect_imports': sorted(
                name for name in sys.modules if name.startswith(('app.services', 'app.agents', 'app.main'))
            ),
        }))
    """)

    assert result["lazy"] is True
    assert result["stable"] is True
    assert set(result["tables"]) == declared_tables()
    assert result["side_effect_imports"] == []


def test_autogenerate_does_not_propose_dropping_existing_orm_tables():
    result = cold_probe("""
        import runpy
        from contextlib import nullcontext
        from importlib import import_module
        from unittest.mock import patch
        from alembic.autogenerate import compare_metadata
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from sqlalchemy import MetaData, create_engine
        with patch('alembic.context.config', Config(), create=True), \\
             patch('alembic.context.is_offline_mode', return_value=True), \\
             patch('alembic.context.configure'), \\
             patch('alembic.context.begin_transaction', return_value=nullcontext()), \\
             patch('alembic.context.run_migrations'):
            namespace = runpy.run_path('alembic/env.py')
        target = MetaData()
        for table in namespace['target_metadata'].sorted_tables:
            table.to_metadata(target)
        for source in Path('app/models').glob('*.py'):
            if not source.name.startswith('_'):
                import_module(f'app.models.{source.stem}')
        from app.core.database import Base
        def memory_only_connect(engine, *args, **kwargs):
            assert str(engine.url) == 'sqlite:///:memory:'
            return original_connect(engine, *args, **kwargs)
        Engine.connect = memory_only_connect
        engine = create_engine('sqlite:///:memory:')
        try:
            Base.metadata.create_all(engine)
            with engine.connect() as connection:
                changes = compare_metadata(MigrationContext.configure(connection), target)
            removed = [change[1].name for change in changes if change[0] == 'remove_table']
            print(json.dumps({'removed': removed, 'change_count': len(changes), 'table_count': len(target.tables)}))
        finally:
            engine.dispose()
    """)

    assert result["table_count"] == len(declared_tables())
    assert result["removed"] == []
    assert result["change_count"] == 0


def test_alembic_retains_the_existing_single_head():
    result = cold_probe("""
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config()
        config.set_main_option('script_location', 'alembic')
        print(json.dumps(ScriptDirectory.from_config(config).get_heads()))
    """)

    assert len(result) == 1
