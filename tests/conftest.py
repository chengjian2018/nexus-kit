"""Pytest tests-dir config: put tests/ itself on sys.path (the shared
fake_provider helper is imported bare by test modules, including the
tests/clarify/ subpackage) and warm up the atom defaults."""

import os
import sys
import tempfile
from pathlib import Path

# Test isolation: when pytest runs from the repo root, CWD probing would hit
# the real host/config/local_config.yaml (which may configure real MCP
# servers) — without disabling it, every test process would actually connect
# to servers (network dependency + the ensure_mcp_ready timing gate slows
# each dialogue round by tens of seconds). Must be set before any
# atoms.tools import.
os.environ.setdefault("NEXUS_MCP_DISABLED", "1")
# Same isolation for the cron scheduler: a real data/cron_jobs.json on the
# dev machine must never be loaded (let alone fired) by a test process.
# Must be set before any atoms.tools import.
os.environ.setdefault("NEXUS_CRON_DISABLED", "1")
# Same isolation for the tool guard's LLM side-channel: a test process must
# never fire background judge-model calls at a real provider. The rule layer
# stays active (pure regex); only the LLM fallback thread is silenced.
os.environ.setdefault("NEXUS_TOOL_GUARD_LLM_DISABLED", "1")

# Same isolation for the app-config layer: apps/archify_agent/config.yaml is
# a real repo file now (design 2026-09-15), and any settings call that is not
# otherwise isolated would read it (llm overlay / loop limits / guardrails /
# custom bag), making "no app config = global behavior" tests
# non-deterministic. An empty tmp apps root pins every test to the global
# defaults by default; tests that need a REAL app config re-point
# NEXUS_APPS_DIR themselves (see tests/test_app_config.py's app_env fixture)
# — with monkeypatch.delenv("NEXUS_APPS_DIR", raising=False) +
# settings.invalidate_config_cache() when the repo file itself is the target.
os.environ.setdefault("NEXUS_APPS_DIR", tempfile.mkdtemp(prefix="nexus_test_apps_"))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import atoms.stages  # noqa: F401,E402 -- registers kernel default-stage factories
import atoms.executors  # noqa: F401,E402 -- registers default executor plugins
