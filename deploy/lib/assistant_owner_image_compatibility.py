"""Identity gate for the reviewed, explicitly disabled 122 foundation.

A matching identity is not evidence that arbitrary external writers or readers
are stopped, nor that enabling ownership is safe. The 121 gate remains required.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import signal

EXPECTED = {
    'app/services/enterprise_assistant_ownership_service.py': 'b4bbb6e64d2119b5bd16531e24f12e2a9a9ebddbc429bbcd930497d333f94598',
    'app/models/enterprise_assistant_ownership.py': 'bbe5669da14230d1b5dffc348f80a3deef354c6740bf53b269956bfe4102365c',
    'app/models/__init__.py': '557057a4bab7de910e3dc2e3133c5c55eb3b3d84f3393f557eaabe8d1b6950c0',
    'app/core/ownership_signals.py': 'ac98c85853637899d1c821782fcb47584745cf69cc549050a5a482f4585066e1',
    'alembic/versions/122_assistant_call_owner.py': 'd89455b9284bda6d7159075d68bed0193d9493b6d8f894aeefa3b420abb8f432',
}


def probe(root: Path) -> bool:
    for path, expected in EXPECTED.items():
        tree = ast.parse((root / path).read_text(encoding='utf-8'))
        digest = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
        if digest != expected:
            return False
        if path.endswith('/enterprise_assistant_ownership_service.py'):
            assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == '_FOUNDATION_ACTIVATED'
                                   for target in node.targets)]
            if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant) or assignments[0].value.value is not False:
                return False
    return True


def main() -> None:
    def timeout(_signum, _frame):
        raise TimeoutError('ownership image probe timeout')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(8)
    try:
        supported = probe(Path(os.environ.get('PRISM_BACKEND_ROOT', '/app')))
    except (OSError, SyntaxError, ValueError, TimeoutError):
        supported = False
    finally:
        signal.alarm(0)
    print('supported' if supported else 'unsupported')


if __name__ == '__main__':
    main()
