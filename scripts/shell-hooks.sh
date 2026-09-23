# agent-memory：pi / codex / claude 启动前先同步共享记忆（由 agent_memory.py init 写入）
_agent_memory_sync() { python3 "/home/tfdx8045/code/agent-memory/scripts/agent_memory.py" sync -q 2>/dev/null || true; }
pi()     { _agent_memory_sync; command pi "$@"; }
codex()  { _agent_memory_sync; command codex "$@"; }
claude() { _agent_memory_sync; command claude "$@"; }
