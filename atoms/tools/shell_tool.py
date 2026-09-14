"""bash / run_python — shell 命令与 Python 代码执行（toolset: shell）.

高权限工具集：授权仍是 deny-by-default 三层收口（注册 toolset →
pattern.allow_toolset → node.use_tools）——
只有 pattern 显式 ``allow_toolset: [shell, ...]`` 且节点
``use_tools`` 点名的 agent 才能触达，注册即全局可用不等于默认暴露::

    pattern:
      allow_toolset: [shell, filesystem, knowledge]
    node:
      use_tools: [bash, run_python]        # 只点本节点需要的

两个工具共用一套子进程护栏：

- ``timeout_seconds``（config ``shell_tool`` 节，默认 60s；args 只能调小）
  —— 超时按进程组 SIGKILL（``start_new_session`` 独立会话，shell 里的
  子孙进程一并回收），返回已产出的部分输出 + ``timed_out: true``。
- ``max_output_chars``（默认 20000）—— stdout / stderr 各自截断回填，
  防单条命令刷爆 agent 上下文。

run_python 用 ``sys.executable -I`` 在临时文件中执行：与宿主同解释器
（依赖可复用），``-I`` isolated mode 隔离 PYTHONPATH / user site，代码
无法借用宿主进程环境做持久化；stdin 关闭（DEVNULL），读输入的代码立即
EOF 而不是挂住等输入。

本工具集是"能力边界在授权层"的取舍：不对命令内容做黑名单过滤
（形同虚设且误伤率极高），工作目录不设沙箱（本地个人 kit 的定位）；
真正收口在 pattern 授权 + 资源护栏。
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

from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_shell_tool_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 子进程执行核心（bash 与 run_python 共用）
# ---------------------------------------------------------------------------

def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL 整个进程组（start_new_session 保证 proc 是组长）。

    进程已退出 / 平台无 killpg 时静默降级到 proc.kill()。
    """
    try:
        if hasattr(os, "killpg") and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass  # 已退出：communicate() 兜底收尸


async def _run_subprocess(argv: Tuple[str, ...], *, shell: bool = False,
                          timeout: float,
                          workdir: Optional[str] = None
                          ) -> Dict[str, Any]:
    """跑一个子进程到结束（或超时），返回未截断的原始结果。

    argv 为完整参数表（shell=False，run_python 用）；shell=True 时
    argv[0] 是整条命令串（bash 用）。stdin 恒为 DEVNULL——交互式命令
    立即 EOF 而非挂住。
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
        stdout_b, stderr_b = await proc.communicate()

    return {
        "exit_code": proc.returncode,
        "stdout": stdout_b.decode("utf-8", errors="replace"),
        "stderr": stderr_b.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }


def _resolve_workdir(args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """校验可选 workdir 参数；返回 (err, workdir)。"""
    raw = args.get("workdir")
    if raw is None or str(raw).strip() == "":
        return None, None
    workdir = Path(str(raw).strip()).expanduser()
    if not workdir.is_dir():
        return (f"workdir 不存在或不是目录: {workdir}", None)
    return None, str(workdir.resolve())


def _resolve_timeout(args: Dict[str, Any], guard: Dict[str, Any]
                     ) -> Tuple[Optional[str], Optional[float]]:
    """超时参数：args 只能调小（与 delegate_task / run_workflow 同哲学）。"""
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
    """输出截断：超限截到 limit 并加标记，返回 (text, truncated)。"""
    if len(text) <= limit:
        return text, False
    marker = f"\n...[输出超长，已截断：完整 {len(text)} 字符，仅保留前 {limit} 字符]"
    return text[:limit] + marker, True


def _payload(raw: Dict[str, Any], guard: Dict[str, Any],
             started: float, **extra: Any) -> str:
    """组装回填 payload：stdout/stderr 各自截断 + 计时。"""
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

    guard = get_shell_tool_config()
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

    guard = get_shell_tool_config()
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
