#!/usr/bin/env python3
"""Check pinned, reviewed 121 evidence reader via conservative AST fingerprints.

This is a release identity gate, not proof that arbitrary matching dependencies are safe.
A reviewed change to any pinned module requires updating these fingerprints and tests.
"""
from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import signal

# Conservative identity gate for reviewed reader modules and direct signal dependencies.
# It does not establish correctness or cover arbitrary unlisted dependencies.
EXPECTED_MODULES = {
    'services/billing_reservation_reconciliation_service.py': 'ea7d3a4b24122b6833739278006d18d520390d5784a5e402c86f84b1edb0f226',
    'services/billing_ledger_service.py': 'd4fb310a97cdc5bd394b6a464f7e347776dd7f7dc9e3ff4ab918ecf628d92a7a',
    'core/ownership_signals.py': 'ac98c85853637899d1c821782fcb47584745cf69cc549050a5a482f4585066e1',
    'services/deepseek_responses_runtime.py': '90f2586eba5c126c12f84d29c3c9595e476cdc82ac02c1b8e2441e9a35b89568',
    'services/agent_responses_service.py': '7890bf381a7d778e9e0eb5af7d8343c2282d891b848784fa57bb4c03432f33cd',
    'services/billing_metered_call.py': '19d686f7f966f97c4b91cbaccb71ce67793781da596b73e36235c9804e62e3c4',
    'services/enterprise_agent_team_runtime_service.py': '554e000c51322443d0908a6afb56097f8ebafebbd51b4a851f656d9d4b533c25',
    'services/enterprise_agent_team_dispatcher.py': '5113607dd9cc15cdc9915a7d2922d718448cabbaad0df8620bce98ffa4933385',
    'services/enterprise_assistant_billing_group.py': 'f67915278451a3bd4658c85fed9dd1e8c6a8fe8bf3cab43e67f43a3213288093',
    'services/rbac_service.py': '6e81c15b515e6725420fcc4538b8ffee46f4e8973c6768cb54fee89d5beafd5d',
    'models/billing_ledger.py': '9a6f515ae2f19e85c011e940059597a0b270c82ea86513526fdcfd24c0a8666a',
    'models/user.py': '86de7d8a3378b24087fc0362f71c1b56516a30e4e32ad1da936c88c56bceee64',
    'api/v1/billing.py': '2348f600c2dc7043d22b1b47be0d1fcbb17d12f28fd9c958e9ada9ad23c908eb',
    'api/v1/enterprise_agent_teams.py': 'aba2c8249e26b29768c30859c218dbf16e054194191b278c5911e0bc9b68ede3',
    'api/__init__.py': 'cc6aa367a02f65af9a856168e9fa68b714ba8433599d3899a75dfdf0f0cc71d4',
    'core/dependencies.py': '218a69b3b0ca1e380b3261619638b90052590b45f2bed868435faa3fce173b11',
    'core/security.py': '4cb6ad192bb5f4a3d0633da8a6cedc64ef3a745ffce7df0a369cd0abe9bbcde9',
    'core/super_admin.py': '5acabc74bd3119c3dec2fcc8023088ad473cd75ee0d0f7d5504dff2f355e0b55',
    'schemas/common.py': 'c61d1d397875b379159ca6418cf2ac3a52acccf52c16bea6aa78de99d993509e',
    'main.py': '0debff8636df3f2fb8759dd1ebd8cf6145e3abfb4cddd252782a383715e02c5d',
    'services/enterprise_assistant_runtime_service.py': '0ce54660b58b712c02ff5f898d0fe3f7044a527c6f3d2927d3417c4c62b58c30',
    'services/billing_search_fetch_service.py': '60e33118c77ec3926fbc2a95ee17f3e1577d318049132d831586e55f6e41973f',
    'services/billing_rate_catalog_service.py': '56b122e417386721f1319965fbbd31dbad0de5a836bf3ab3d0abfbbb2c0112c2',
    'services/enterprise_org_service.py': '7e7e62db935b6e71d5d0efa319b0746ce0f282d18e2d50399e7799e09ad9162e',
    'services/enterprise_chat_service.py': '6fca6402e731ecead8622658de5afe48b79ccf475ce02d8b0cf8ef2d1bcd6fe8',
    'core/database.py': 'b27fd309796dfe2304bdc7ad6f74fe6de196623981fd858a8f5c327cdf8680df',
    'models/enterprise_agent_team.py': 'b717368e56e5f1e0a6060063fbef51a2b910ea17f1993d33d78aaa979479cd83',
    'models/enterprise_assistant_runtime.py': '2f2b8f7c30d0b73a76d18aea8690664e3d8ffb41988be091093104ecc56d346d',
    'models/enterprise_chat.py': '017d9e0892501393489c59c727f8f2486f6c5c56824e8a0bc441bf7cc5af6e61',
    'models/enterprise_org.py': 'fece3e307c381f95758a97b570e39ec4736e56120542e09e2dd0ea1d2478ad3f',
    'services/billing_runtime_gate.py': '3c500c42843c36b31b74983a3cae6c1b862c9ac7f8f20a33d5866a5c45beee13',
    'models/rbac.py': 'f6e6b5a9eb36e70236c051677adfdda4eda2e9cd6d2b63bd89a4dfabb525b699',
}


def probe(root: pathlib.Path) -> bool:
    # Parse one module at a time to fit the isolated image probe memory cap.
    for path, expected in EXPECTED_MODULES.items():
        module = ast.parse((root / path).read_text(encoding="utf-8"))
        digest = hashlib.sha256(ast.dump(module, include_attributes=False).encode()).hexdigest()
        if digest != expected:
            return False
    return True



def main():
    def timeout(_signum, _frame):
        raise TimeoutError("billing evidence probe deadline exceeded")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(8)
    try:
        supported = probe(pathlib.Path(os.environ.get("PRISM_APP_ROOT", "/app/app")))
    except (OSError, SyntaxError, ValueError, TimeoutError):
        supported = False
    finally:
        signal.alarm(0)
    print("supported" if supported else "unsupported")


if __name__ == "__main__":
    main()
