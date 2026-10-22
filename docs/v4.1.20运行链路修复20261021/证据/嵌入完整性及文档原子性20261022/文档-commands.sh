cd '/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/.claude/worktrees/great-heisenberg-8a6fa9'
backend/.venv/bin/python -B '/tmp/prism-agent-knowledge-atomic-20261022-141304/run_tests.py' red > '/tmp/prism-agent-knowledge-atomic-20261022-141304/red.log' 2>&1
backend/.venv/bin/python -B '/tmp/prism-agent-knowledge-atomic-20261022-141304/run_tests.py' green > '/tmp/prism-agent-knowledge-atomic-20261022-141304/green.log' 2>&1
backend/.venv/bin/python -B '/tmp/prism-agent-knowledge-atomic-20261022-141304/run_approval.py' green > '/tmp/prism-agent-knowledge-atomic-20261022-141304/approval.log' 2>&1
backend/.venv/bin/ruff check --no-cache backend/app/services/agent_knowledge_service.py backend/tests/unit/services/test_embedding_usage_recording.py
git diff --check -- backend/app/services/agent_knowledge_service.py backend/tests/unit/services/test_embedding_usage_recording.py
