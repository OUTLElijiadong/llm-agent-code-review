#!/usr/bin/env bash
set -euo pipefail
auditEvidenceDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
auditRepoRoot="$(git -C "$auditEvidenceDir" rev-parse --show-toplevel)"
auditFrontendRoot="$auditRepoRoot/frontend"
auditTempRoot="$(mktemp -d /tmp/prism-frontend-repro.XXXXXX)"
auditTempRoot="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$auditTempRoot")"
cp "$auditEvidenceDir"/*.test.ts "$auditTempRoot/"
ln -s "$auditFrontendRoot/node_modules" "$auditTempRoot/node_modules"
python3 - "$auditFrontendRoot" "$auditTempRoot" <<'PYCONFIG'
import json,sys
from pathlib import Path
frontend,temp=map(Path,sys.argv[1:])
config="import base from "+json.dumps(str(frontend/'vitest.config.ts'),ensure_ascii=False)+"\n"
config += "export default { ...base, root: "+json.dumps(str(temp))+", cacheDir: "+json.dumps(str(temp/'cache'))+", test: { ...base.test, setupFiles: ["+json.dumps(str(frontend/'src/test/setup.ts'),ensure_ascii=False)+"], include: ['*.test.ts'], coverage: { enabled: false } } }\n"
(temp/'vitest.config.mts').write_text(config)
PYCONFIG
node "$auditFrontendRoot/node_modules/vitest/vitest.mjs" run --config "$auditTempRoot/vitest.config.mts" --reporter verbose "$@"
