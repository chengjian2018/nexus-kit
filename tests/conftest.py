"""Pytest tests-dir config: put tests/ itself on sys.path (the shared
fake_provider helper is imported bare by test modules, including the
tests/clarify/ subpackage) and warm up the atom defaults."""

import os
import sys
from pathlib import Path

# 测试隔离:pytest 从仓库根运行时,CWD 探测会命中真实的
# host/config/local_config.yaml(可能配置了真实 MCP server)——不关掉则
# 每个测试进程都会真连 server(网络依赖 + ensure_mcp_ready 时序闸把每轮
# 对话拖慢数十秒)。必须在任何 atoms.tools import 之前置位
os.environ.setdefault("NEXUS_MCP_DISABLED", "1")

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import atoms.stages  # noqa: F401,E402 -- registers kernel default-stage factories
import atoms.executors  # noqa: F401,E402 -- registers default executor plugins
