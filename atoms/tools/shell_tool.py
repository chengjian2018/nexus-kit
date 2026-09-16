"""bash / run_python — shell command and Python code execution (toolset: shell).

High-privilege toolset: authorization is still the deny-by-default
three-layer gate (registered toolset → pattern.allow_toolset →
node.use_tools) — only agents whose pattern explicitly declares
``allow_toolset: [shell, ...]`` AND whose node names the tools in
``use_tools`` can reach them; registering globally-available does not mean
exposed by default::

    pattern:
      allow_toolset: [shell, filesystem, knowledge]
    node:
      use_tools: [bash, run_python]        # only what this node needs

Both tools share one subprocess guardrail set:

- ``timeout_seconds`` (config ``shell_tool`` section, default 60s; args may
  only lower it) — timeout kills the whole process group with SIGKILL
  (``start_new_session`` gives the proc its own session, so descendant
  processes in the shell are reaped too) and returns the partial output
  already produced + ``timed_out: true``.
- ``max_output_chars`` (default 20000) — stdout / stderr each truncated
  before backfill, so one command cannot flood the agent context.

run_python executes from a temp file via ``sys.executable -I``: the same
interpreter as the host (dependencies reusable), ``-I`` isolated mode
separates PYTHONPATH / user site so code cannot lean on the host process
environment for persistence; stdin is closed (DEVNULL) — input-reading code
hits EOF immediately instead of hanging.

This toolset embodies the "capability boundary at the authorization layer"
trade-off: no blacklist filtering of command content (security theater with
a huge false-positive rate) and no working-directory sandbox (a local
personal kit by positioning); the real gate is pattern authorization +
resource guardrails.
"""

import asyncio
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from nexus.engine.tool_context import ambient_pattern_code
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_shell_tool_config

logger = logging.getLogger(__name__)

# Post-kill reaping grace (seconds): enough to read back residual pipe
# output, yet short enough that an already-timed-out tool call does not
# occupy another uncancelled to_thread thread
_POST_KILL_GRACE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Subprocess execution core (shared by bash and run_python)
# ---------------------------------------------------------------------------

def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole process group (start_new_session guarantees proc is the leader).

    Silently degrades to proc.kill() when the process already exited or the
    platform lacks killpg.
    """
    try:
        if hasattr(os, "killpg") and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass  # already exited: communicate() reaps as a fallback


async def _run_subprocess(argv: Tuple[str, ...], *, shell: bool = False,
                          timeout: float,
                          workdir: Optional[str] = None
                          ) -> Dict[str, Any]:
    """Run a subprocess to completion (or timeout), returning the untruncated raw result.

    argv is the full argument vector (shell=False, used by run_python);
    shell=True means argv[0] is the whole command string (used by bash).
    stdin is always DEVNULL — interactive commands hit EOF immediately
    instead of hanging.
    """
    kwargs: Dict[str, Any] = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=workdir,
        start_new_session=True,
    )
    if shell:
        proc = await asyncio.create_subprocess_shell(argv[0], **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_process_group(proc)
        # The reaping also gets a grace window: killpg cannot reach
        # setsid-escaped grandchildren (nohup / double-fork daemons) — they
        # hold the pipe write end and communicate never sees EOF. Past the
        # grace, force-close the stdio transport and give up the residual
        # output; never hang "after the timeout".
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_POST_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "子进程超时击杀后 %.1fs 仍未收尸（孙进程握管道），放弃残余输出",
                _POST_KILL_GRACE_SECONDS)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001 -- best-effort teardown
                        pass
            stdout_b, stderr_b = b"", b""

    return {
        "exit_code": proc.returncode,
        "stdout": stdout_b.decode("utf-8", errors="replace"),
        "stderr": stderr_b.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }


def _resolve_workdir(args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Validate the optional workdir arg; returns (err, workdir)."""
    raw = args.get("workdir")
    if raw is None or str(raw).strip() == "":
        return None, None
    workdir = Path(str(raw).strip()).expanduser()
    if not workdir.is_dir():
        return (f"workdir 不存在或不是目录: {workdir}", None)
    return None, str(workdir.resolve())


def _resolve_timeout(args: Dict[str, Any], guard: Dict[str, Any]
                     ) -> Tuple[Optional[str], Optional[float]]:
    """Timeout arg: args may only lower it (same philosophy as delegate_task / run_workflow)."""
    timeout = float(guard["timeout_seconds"])
    if args.get("timeout_seconds") is not None:
        try:
            requested = float(args["timeout_seconds"])
        except (TypeError, ValueError):
            return "timeout_seconds 应为正数（秒）", None
        if requested <= 0:
            return "timeout_seconds 必须大于 0", None
        timeout = min(requested, timeout)
    return None, timeout


def _clip_output(text: str, limit: int) -> Tuple[str, bool]:
    """Output truncation: over-limit clips to limit with a marker; returns (text, truncated)."""
    if len(text) <= limit:
        return text, False
    marker = f"\n...[输出超长，已截断：完整 {len(text)} 字符，仅保留前 {limit} 字符]"
    return text[:limit] + marker, True


def _payload(raw: Dict[str, Any], guard: Dict[str, Any],
             started: float, **extra: Any) -> str:
    """Assemble the backfill payload: stdout/stderr each truncated + timing."""
    limit = int(guard["max_output_chars"])
    stdout, out_cut = _clip_output(raw["stdout"], limit)
    stderr, err_cut = _clip_output(raw["stderr"], limit)
    payload: Dict[str, Any] = {
        "exit_code": raw["exit_code"],
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": raw["timed_out"],
        "truncated": out_cut or err_cut,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    payload.update(extra)
    if raw["timed_out"]:
        payload["note"] = "进程超时被终止（SIGKILL 整个进程组）；输出为已产出的部分结果"
    return tool_result(payload)


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

BASH_SCHEMA = {
    "name": "bash",
    "description": (
        "执行一条 shell 命令并返回 stdout/stderr/退出码。适合文件操作、"
        "git、构建脚本、系统信息查询等。长时间命令会被超时终止"
        "（可选 timeout_seconds 只能调小）；输出超长会截断。"
        "命令以非交互方式运行（无 stdin），需要交互的程序会立即退出。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令（单条，可含管道与 && 串联）",
            },
            "workdir": {
                "type": "string",
                "description": "工作目录（可选；缺省为服务启动目录；必须是已存在的目录）",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "超时秒数（可选；只能调小，不能超过系统上限）",
            },
        },
        "required": ["command"],
    },
}


async def _handle_bash(args: Dict[str, Any]) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return tool_error("command 必填：要执行的 shell 命令")

    guard = get_shell_tool_config(ambient_pattern_code())
    err, workdir = _resolve_workdir(args)
    if err:
        return tool_error(err)
    err, timeout = _resolve_timeout(args, guard)
    if err:
        return tool_error(err)

    logger.info("[bash] exec: timeout=%.0fs, workdir=%s, cmd=%r",
                timeout, workdir or "<cwd>", command[:120])
    started = time.monotonic()
    try:
        raw = await _run_subprocess((command,), shell=True,
                                     timeout=timeout, workdir=workdir)
    except FileNotFoundError as e:
        return tool_error(f"无法启动 shell: {e}")
    return _payload(raw, guard, started, command=command)


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------

RUN_PYTHON_SCHEMA = {
    "name": "run_python",
    "description": (
        "执行一段 Python 代码并返回 stdout/stderr/退出码。代码写入临时"
        "文件、由独立子进程运行（与宿主同解释器、依赖可复用），进程结束"
        "后临时文件即删除。适合数据处理、算法验证、批量转换等不适合写成"
        "shell 的任务。print 的内容出现在 stdout；未捕获异常的 traceback "
        "出现在 stderr。长时间运行会被超时终止。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 源码（脚本顶层，非函数体）",
            },
            "workdir": {
                "type": "string",
                "description": (
                    "子进程工作目录（可选；相对路径的文件读写以此为基准，"
                    "缺省为服务启动目录）"
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "超时秒数（可选；只能调小，不能超过系统上限）",
            },
        },
        "required": ["code"],
    },
}


async def _handle_run_python(args: Dict[str, Any]) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return tool_error("code 必填：要执行的 Python 源码")

    guard = get_shell_tool_config(ambient_pattern_code())
    err, workdir = _resolve_workdir(args)
    if err:
        return tool_error(err)
    err, timeout = _resolve_timeout(args, guard)
    if err:
        return tool_error(err)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False)
    try:
        with tmp:
            tmp.write(code)
        logger.info("[run_python] exec: timeout=%.0fs, workdir=%s, %d chars",
                    timeout, workdir or "<cwd>", len(code))
        started = time.monotonic()
        try:
            raw = await _run_subprocess(
                (sys.executable, "-I", tmp.name),
                timeout=timeout, workdir=workdir)
        except FileNotFoundError as e:
            return tool_error(f"无法启动 Python 解释器: {e}")
        return _payload(raw, guard, started)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Self-registration (registered on module import; AST scan auto-discovery)
# ---------------------------------------------------------------------------

registry.register(
    name="bash",
    toolset="shell",
    schema=BASH_SCHEMA,
    handler=_handle_bash,
    is_async=True,
    description="执行 shell 命令（stdout/stderr/退出码，超时与截断护栏）",
    emoji="💻",
)

registry.register(
    name="run_python",
    toolset="shell",
    schema=RUN_PYTHON_SCHEMA,
    handler=_handle_run_python,
    is_async=True,
    description="执行 Python 代码（独立子进程，同解释器依赖）",
    emoji="🐍",
)
