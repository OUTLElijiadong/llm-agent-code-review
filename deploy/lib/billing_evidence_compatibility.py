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

# Reviewed with dual-approval, evidence provenance and provider-change regression tests.
EXPECTED_MODULES = {
    'services/billing_reservation_reconciliation_service.py': 'ea7d3a4b24122b6833739278006d18d520390d5784a5e402c86f84b1edb0f226',
    'services/billing_ledger_service.py': 'd4fb310a97cdc5bd394b6a464f7e347776dd7f7dc9e3ff4ab918ecf628d92a7a',
    'services/billing_metered_call.py': 'e188b657c8e9e90bfe3c5baab504cc3e1fa0f3265847f8f20c3f5712c7c243a3',
    'services/billing_statement_gateway.py': '07b3d66351753bee9ebcc5d31466db9a44ce38f7306f7bb624206d9cb050a2bd',
    'services/enterprise_agent_team_runtime_service.py': 'f7d86d5c1de83d96a06677eab59eb72aecee3b72f3b35fad9fff8f10915a135f',
    'services/enterprise_agent_team_dispatcher.py': 'e90afbf0bf7b0c00f9d9bae6c2e6273de013d59a943e488e6c89dea1fe4bc66e',
    'services/enterprise_assistant_billing_group.py': 'f67915278451a3bd4658c85fed9dd1e8c6a8fe8bf3cab43e67f43a3213288093',
    'services/rbac_service.py': '6e81c15b515e6725420fcc4538b8ffee46f4e8973c6768cb54fee89d5beafd5d',
    'models/billing_ledger.py': '9a6f515ae2f19e85c011e940059597a0b270c82ea86513526fdcfd24c0a8666a',
    'models/user.py': '86de7d8a3378b24087fc0362f71c1b56516a30e4e32ad1da936c88c56bceee64',
    'api/v1/billing.py': '3186425eab84d9f70d54da396ab064cc6c6f7acbfbc667f5a03aeb76477b0889',
    'api/v1/enterprise_agent_teams.py': 'aba2c8249e26b29768c30859c218dbf16e054194191b278c5911e0bc9b68ede3',
    'api/__init__.py': 'cc6aa367a02f65af9a856168e9fa68b714ba8433599d3899a75dfdf0f0cc71d4',
    'core/dependencies.py': '218a69b3b0ca1e380b3261619638b90052590b45f2bed868435faa3fce173b11',
    'core/security.py': '4cb6ad192bb5f4a3d0633da8a6cedc64ef3a745ffce7df0a369cd0abe9bbcde9',
    'core/super_admin.py': '5acabc74bd3119c3dec2fcc8023088ad473cd75ee0d0f7d5504dff2f355e0b55',
    'schemas/common.py': 'c61d1d397875b379159ca6418cf2ac3a52acccf52c16bea6aa78de99d993509e',
    'main.py': '3d428170583111645a53c8e083b003ffa3025cb37f30ac26e0a1f71bf5493cc6',
    'core/error_handlers.py': '772b02866283691617cee1748227af6eef6abbd69188fc50cb89caf1c0efb930',
    'core/uvicorn_log_filter.py': 'de73f5eafc59e05b3968f67c08082f708ced0d32410f4be7b35e84c75350bcb7',
    'services/enterprise_assistant_runtime_service.py': '5f55251dae0c1adf7ad66fece72f3e72066fdbc14ebd761afe8f51cf8fb9a5e4',
    'services/billing_search_fetch_service.py': 'cc9d988b86578c4c4ae6a0ff119349fc9ca0ca3d761154b5ad6f3fa4a1065e8d',
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

# Review these modules as complete combinations, including the billing API
# and its statement gateway; their call signatures must remain compatible.
# 4.1.53 used the old entry and API; 4.1.54/55 use the new entry and old API;
# the sandbox candidate uses the new entry and the reviewed new API.
REVIEWED_ENTRY_PROFILES = {
    (
        '0debff8636df3f2fb8759dd1ebd8cf6145e3abfb4cddd252782a383715e02c5d',
        '33e6b79d3e605b840564af8e26da07b16e2246506df5cd65476ad96d0b3c711c',
        None,
        '2348f600c2dc7043d22b1b47be0d1fcbb17d12f28fd9c958e9ada9ad23c908eb',
        '332eabeffd61f97eee705a413e19a6decf00caaf04ab536e7a8e136cbeda4d84',
    ),
    (
        EXPECTED_MODULES['main.py'],
        EXPECTED_MODULES['core/error_handlers.py'],
        EXPECTED_MODULES['core/uvicorn_log_filter.py'],
        '2348f600c2dc7043d22b1b47be0d1fcbb17d12f28fd9c958e9ada9ad23c908eb',
        '332eabeffd61f97eee705a413e19a6decf00caaf04ab536e7a8e136cbeda4d84',
    ),
    (
        EXPECTED_MODULES['main.py'],
        EXPECTED_MODULES['core/error_handlers.py'],
        EXPECTED_MODULES['core/uvicorn_log_filter.py'],
        EXPECTED_MODULES['api/v1/billing.py'],
        EXPECTED_MODULES['services/billing_statement_gateway.py'],
    ),
}
PROFILE_MODULES = ('main.py', 'core/error_handlers.py', 'core/uvicorn_log_filter.py', 'api/v1/billing.py',
                   'services/billing_statement_gateway.py')


def module_digest(path: pathlib.Path) -> str:
    module = ast.parse(path.read_text(encoding='utf-8'))
    return hashlib.sha256(ast.dump(module, include_attributes=False).encode()).hexdigest()


def probe(root: pathlib.Path) -> bool:
    # Parse one module at a time to fit the isolated image probe memory cap.
    for path, expected in EXPECTED_MODULES.items():
        if path in PROFILE_MODULES:
            continue
        if module_digest(root / path) != expected:
            return False
    main, handlers, hook, billing, statement = (root / path for path in PROFILE_MODULES)
    hook_digest = module_digest(hook) if os.path.lexists(hook) else None
    return (module_digest(main), module_digest(handlers), hook_digest, module_digest(billing),
            module_digest(statement)) in REVIEWED_ENTRY_PROFILES



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
