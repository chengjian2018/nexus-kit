"""bash / run_python (shell tool) unit tests: subprocess execution, exit code/stdout/stderr
backfill, timeout process-group kill, output truncation, args-may-only-lower
timeout, workdir validation, guardrail config reads, and registry-level
registration ownership (toolset: shell).
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from async_utils import arun

from atoms.tools import shell_tool  # noqa: F401 -- the module import registers
from nexus.registry.tools import registry as tool_registry

_GUARD = {"timeout_seconds": 8, "max_output_chars": 20000}


def _dispatch(name, args, guard=None):
    """Execute via registry.dispatch (async handlers get awaited — the same contract as the production path)."""
    with patch("atoms.tools.shell_tool.get_shell_tool_config",
               return_value=dict(guard or _GUARD)):
        return json.loads(arun(tool_registry.dispatch(name, args)))


def _bash(args, guard=None):
    return _dispatch("bash", args, guard)


def _py(args, guard=None):
    return _dispatch("run_python", args, guard)


# ---------------------------------------------------------------------------
# Registration ownership
# ---------------------------------------------------------------------------

def test_registered_in_shell_toolset():
    assert tool_registry.get_toolset_for_tool("bash") == "shell"
    assert tool_registry.get_toolset_for_tool("run_python") == "shell"
    assert {"bash", "run_python"} <= tool_registry.names_in_toolsets({"shell"})


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

def test_bash_stdout_and_exit_code():
    r = _bash({"command": "echo hello"})
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "hello"
    assert r["timed_out"] is False
    assert r["truncated"] is False


def test_bash_nonzero_exit_and_stderr():
    r = _bash({"command": "echo oops >&2; exit 3"})
    assert r["exit_code"] == 3
    assert "oops" in r["stderr"]


def test_bash_timeout_kills_process():
    # Timeout (args 1s < guard 8s): the timed_out marker + the process killed (non-zero exit)
    r = _bash({"command": "sleep 30", "timeout_seconds": 1})
    assert r["timed_out"] is True
    assert r["exit_code"] != 0
    assert "超时" in r.get("note", "")


def test_bash_timeout_args_cannot_exceed_config_cap():
    # args over the config cap are clamped to it (sleep 5 is killed at 2s)
    r = _bash({"command": "sleep 5", "timeout_seconds": 60},
              guard={"timeout_seconds": 2, "max_output_chars": 20000})
    assert r["timed_out"] is True


def test_bash_output_truncation():
    r = _bash({"command": "seq 1 100"},
              guard={"timeout_seconds": 8, "max_output_chars": 50})
    assert r["truncated"] is True
    assert len(r["stdout"]) < 300
    assert "截断" in r["stdout"]


def test_bash_workdir():
    with tempfile.TemporaryDirectory() as td:
        r = _bash({"command": "pwd", "workdir": td})
        assert r["exit_code"] == 0
        # macOS's /var is a symlink to /private/var: assert against the resolved path
        assert r["stdout"].strip() == str(Path(td).resolve())


def test_bash_errors():
    r = _bash({"command": "pwd", "workdir": "/definitely/not/exist"})
    assert "workdir" in r["error"]
    r = _bash({"command": "  "})
    assert "command" in r["error"]
    r = _bash({"command": "echo x", "timeout_seconds": -1})
    assert "timeout_seconds" in r["error"]


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------

def test_run_python_basic():
    r = _py({"code": "print(1 + 1)"})
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "2"


def test_run_python_traceback_in_stderr():
    r = _py({"code": "raise ValueError('boom')"})
    assert r["exit_code"] != 0
    assert "ValueError" in r["stderr"]
    assert "boom" in r["stderr"]


def test_run_python_same_interpreter_and_workdir():
    r = _py({"code": "import sys; print(sys.version_info[0])"})
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "3"

    with tempfile.TemporaryDirectory() as td:
        r = _py({"code": "import os; print(os.getcwd())", "workdir": td})
        assert r["exit_code"] == 0
        assert r["stdout"].strip() == str(Path(td).resolve())


def test_run_python_timeout():
    r = _py({"code": "import time; time.sleep(30)", "timeout_seconds": 1})
    assert r["timed_out"] is True


def test_run_python_empty_code_rejected():
    r = _py({"code": "   "})
    assert "code" in r["error"]


def test_run_python_no_stdin_hang():
    # stdin is DEVNULL: input() fails immediately on EOF instead of hanging for input
    r = _py({"code": "input()"})
    assert r["exit_code"] != 0
    assert r["timed_out"] is False


def test_bash_timeout_returns_despite_detached_grandchild(monkeypatch):
    """A setsid-escaped grandchild holds the pipe write end: the post-kill reaping grace is the backstop —
    the call must return (exit_code/output may be missing) instead of
    hanging forever "after the timeout" on an uncancelable to_thread
    thread."""
    import atoms.tools.shell_tool as st

    monkeypatch.setattr(st, "_POST_KILL_GRACE_SECONDS", 0.5)
    cmd = ('python3 -c "import os,time; os.setsid(); time.sleep(60)" & '
           'exec sleep 60')
    r = _bash({"command": cmd}, guard={**_GUARD, "timeout_seconds": 1})
    assert r["timed_out"] is True
