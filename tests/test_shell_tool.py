"""bash / run_python（shell tool）单测：子进程执行、退出码/stdout/stderr
回填、超时进程组击杀、输出截断、timeout 只能调小、workdir 校验、护栏
config 读取，以及 registry 层面的注册归属（toolset: shell）。
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from async_utils import arun

from atoms.tools import shell_tool  # noqa: F401 -- module import 即注册
from nexus.registry.tools import registry as tool_registry

_GUARD = {"timeout_seconds": 8, "max_output_chars": 20000}


def _dispatch(name, args, guard=None):
    """经 registry.dispatch 执行（异步 handler 被 await，契约同生产路径）。"""
    with patch("atoms.tools.shell_tool.get_shell_tool_config",
               return_value=dict(guard or _GUARD)):
        return json.loads(arun(tool_registry.dispatch(name, args)))


def _bash(args, guard=None):
    return _dispatch("bash", args, guard)


def _py(args, guard=None):
    return _dispatch("run_python", args, guard)


# ---------------------------------------------------------------------------
# 注册归属
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
    # 超时（args 1s < guard 8s）：timed_out 标记 + 进程被杀（非零退出）
    r = _bash({"command": "sleep 30", "timeout_seconds": 1})
    assert r["timed_out"] is True
    assert r["exit_code"] != 0
    assert "超时" in r.get("note", "")


def test_bash_timeout_args_cannot_exceed_config_cap():
    # args 超过 config 上限时按 config 收口（sleep 5 在 2s 处被杀）
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
        # macOS 的 /var 是 /private/var 的 symlink：按 resolved 路径断言
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
    # stdin 为 DEVNULL：input() 立即 EOF 报错，而不是挂住等输入
    r = _py({"code": "input()"})
    assert r["exit_code"] != 0
    assert r["timed_out"] is False
