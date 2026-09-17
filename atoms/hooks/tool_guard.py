"""tool_guard — P4 pre-execution announce of dangerous tool calls
(kind="agent_hooks").

Borrowing Claude Code's bash permission analysis (command segmentation +
rule hits + a secondary small-model review) and hermes's declarative
guardrail style, packaged as an agent_hooks plugin:

- **Rule layer (synchronous, microsecond-scale)**: a compiled regex rule
  table scans the ``bash`` command per tool (whole string first, then
  segmented on ``; / && / || / | / newline`` so both pipe rules and segment
  rules hit), the ``run_python`` code, ``write_text`` / ``edit_file``
  paths, and ``create_cron``.
- **LLM review layer (async, bypass)**: when no high-risk rule hit but the
  command carries suspicious signals (network verbs / pipes / command
  substitution / stealth encoding), the call is submitted to a background
  single-thread analyzer that asks a lightweight model for a
  ``{"risk": ..., "reason": ...}`` JSON verdict. The review never blocks
  the main loop: the hook only enqueues on the event-loop thread, the LLM
  call runs independently via ``asyncio.run`` inside the worker thread,
  and a full queue drops the new entry.

v1 contract ("announce first, never gate"):

- the hook always returns ``None`` (P4 semantics = no name/args rewrite);
- subagent / workflow runs are not scanned — P4 is structurally not on
  ``_run_sub_agent``'s path (the sub loop dispatches the registry
  directly); reading the ``current_tool_context()`` flags here again is a
  second line of defense;
- hook exceptions are swallowed by the agent_hooks dispatcher, and this
  module also backstops internally (unreadable config → defaults; LLM
  failure → silent drop); the announce channels are only the log and the
  in-memory ledger (``recent_findings()``, consumed by studio / tests).

Enablement (pattern-level declaration, aligned with the allow_toolset
authorization philosophy — whoever grants shell/filesystem hangs the
guard)::

    Pattern(..., plugins={"agent_hooks": "tool_guard"}, ...)

Config section ``tool_guard`` (optional, see host/config/local_config.yaml):
enabled / llm_fallback / llm_max_input_chars / llm_max_queue /
llm_timeout_seconds / llm (``llm`` is a judge-model override layered on top
of the ambient connection — point it at a cheap small model).
"""

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nexus.engine.tool_context import current_tool_context
from nexus.registry.plugins import registry

logger = logging.getLogger(__name__)

# Test-isolation switch (conftest sets it to 1): kills the LLM review bypass thread path
LLM_DISABLED_ENV = "NEXUS_TOOL_GUARD_LLM_DISABLED"

# ============================================================================
# Data shapes for verdicts and findings
# ============================================================================

SEV_HIGH = "high"
SEV_MEDIUM = "medium"
SEV_LOW = "low"

_SEV_LOG_LEVEL = {
    SEV_HIGH: logging.WARNING,
    SEV_MEDIUM: logging.WARNING,
    SEV_LOW: logging.INFO,
}
_SEV_TAG = {
    SEV_HIGH: "🔴 高风险",
    SEV_MEDIUM: "🟠 中风险",
    SEV_LOW: "🟡 低风险",
}


@dataclass(frozen=True)
class GuardFinding:
    """One hit (rule or LLM verdict share the shape, so ledger / UI consume them uniformly)."""

    tool: str
    rule_id: str          # e.g. "shell.rm-force" / "llm.verdict"
    severity: str         # high | medium | low
    summary: str          # one-line danger characterization
    evidence: str         # the matched fragment (already truncated)
    session_id: str = ""
    node_code: str = ""
    source: str = "rule"  # rule | llm


# ============================================================================
# Rule table (all regexes compiled once at module load)
# ============================================================================

# --- bash: segment delimiters. Note that pipe rules like "curl … | sh" must
# --- match on the whole string, while segment rules like "rm -rf" match
# --- after segmentation — both tables run, deduped by rule_id.
# --- rm's recursive+force combo also goes through the _scan_rm_dangerous
# --- structured detection (regexes are blind to split flags / long flags /
# --- uppercase -R), also deduped by rule_id.
_SHELL_SPLIT_RE = re.compile(r"\n|&&|\|\||;|\|")

_RM_RF_RULE_ID = "shell.rm-recursive-force"
_RM_RF_SUMMARY = "递归强制删除（rm -rf 族，误伤不可逆）"
# Combined-flag forms squeezing r and f into one token (-rf/-fr/-Rf/-rvf…) —
# the fallback layer when the structured detection's shlex fails (unbalanced
# quotes); the normal path hits this first, quickly
_RM_RF_RE = re.compile(r"\brm\b[^|;&\n]*\s-[a-zA-Z]*r[a-zA-Z]*f\b"
                       r"|\brm\b[^|;&\n]*\s-[a-zA-Z]*f[a-zA-Z]*r\b")

# (rule_id, severity, pattern, summary) — whole string first, then segments
_SHELL_RULES: List[tuple] = [
    # ---- high: destruction / privilege-escalation surfaces / remote history rewrite ----
    (_RM_RF_RULE_ID, SEV_HIGH, _RM_RF_RE, _RM_RF_SUMMARY),
    ("shell.pipe-to-shell", SEV_HIGH,
     re.compile(r"\b(?:curl|wget|base64|openssl|echo|printf)\b[^|;&\n]*\|"
                r"\s*(?:sudo\s+)?(?:ba|z|fi|da)?sh\b"),
     "下载/解码内容直接管道进 shell 执行"),
    ("shell.reverse-shell", SEV_HIGH,
     re.compile(r"/dev/tcp/|\bbash\s+-i\s+>&|\bnc(?:at)?\b[^|;&\n]*\s-e\b"),
     "反弹 shell 特征（/dev/tcp、nc -e、bash -i 重定向）"),
    ("shell.mkfs-dd-device", SEV_HIGH,
     re.compile(r"\bmkfs(?:\.\w+)?\b|\bdd\b[^|;&\n]*\bof=/dev/"
                r"|>{1,2}\s*/dev/(?:sd|nvme|hd)"),
     "块设备级写入/格式化（磁盘不可逆破坏）"),
    ("shell.fork-bomb", SEV_HIGH,
     re.compile(r":\s*\(\)\s*\{"),
     "fork 炸弹函数定义特征"),
    ("shell.sudo", SEV_HIGH,
     re.compile(r"^(?:sudo\b|su\s+-)"),
     "提权执行（sudo / su -）"),
    ("shell.shutdown", SEV_HIGH,
     re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"),
     "关机/重启类系统级操作"),
    ("shell.git-force-push", SEV_HIGH,
     re.compile(r"\bgit\s+push\b[^|;&\n]*\s(?:--force(?!-with-lease)|-f\b)"),
     "强推远端（覆盖他人提交历史）"),
    ("shell.write-etc", SEV_HIGH,
     re.compile(r">{1,2}\s*/{1,2}etc/"
                r"|\btee\b[^|;&\n]*\s/{1,2}etc/"
                r"|\b(?:cp|mv|install|rsync)\b[^|;&\n]*\s/{1,2}etc/"
                r"|\bsed\b[^|;&\n]*\s-[a-zA-Z]*i\b[^|;&\n]*\s/{1,2}etc/"),
     "写入 /etc（系统配置改写：重定向/tee/cp/mv/install/rsync/sed -i）"),
    ("shell.write-shell-rc", SEV_HIGH,
     re.compile(r"(?:>{1,2}|tee\s+(?:-a\s+)?)[^|;&\n]*"
                r"\.(?:bashrc|zshrc|bash_profile|zprofile|zlogin)\b"),
     "写入 shell 启动脚本（登录即执行的持久化面）"),
    ("shell.chmod-system", SEV_HIGH,
     re.compile(r"\bchmod\b[^|;&\n]*(?:/etc|/usr|/System|/bin)\b"),
     "对系统目录改权限"),
    # ---- medium: irreversible / persistence / out-of-scope surfaces ----
    ("shell.git-reset-hard", SEV_MEDIUM,
     re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f)"),
     "本地历史/工作区硬清除（未提交内容丢失）"),
    ("shell.git-force-with-lease", SEV_MEDIUM,
     re.compile(r"\bgit\s+push\b[^|;&\n]*\s--force-with-lease\b"),
     "带保护的重推（仍覆盖远端历史）"),
    ("shell.kill-broad", SEV_MEDIUM,
     re.compile(r"\bkill(?:all)?\b[^|;&\n]*\s-1\b|\bpkill\b[^|;&\n]*\s-f\b"),
     "广谱杀进程（kill -1 / pkill -f）"),
    ("shell.crontab", SEV_MEDIUM,
     re.compile(r"\bcrontab\b|\blaunchctl\s+(?:load|bootstrap)\b"),
     "定时/开机自项持久化（crontab / launchctl）"),
    ("shell.read-secrets", SEV_MEDIUM,
     re.compile(r"\b(?:cat|head|tail|less|more|cp|scp|rsync)\b[^|;&\n]*"
                r"(?:id_rsa|id_ed25519|\.pem\b|\.env\b|\.netrc"
                r"|\.aws/credentials|\.kube/config)"),
     "读取密钥/凭据类文件"),
    ("shell.env-exfil", SEV_MEDIUM,
     re.compile(r"\b(?:env|printenv)\b[^|;&\n]*\|"),
     "环境变量整包外送（可能含密钥）"),
    ("shell.chmod-777", SEV_MEDIUM,
     re.compile(r"\bchmod\b[^|;&\n]*\s777\b"),
     "777 全开权限"),
    ("shell.docker-privileged", SEV_MEDIUM,
     re.compile(r"\bdocker\b[^|;&\n]*--privileged\b"
                r"|\bdocker\s+system\s+prune\b"),
     "容器提权运行 / docker 全量清理"),
    ("shell.rsync-delete", SEV_MEDIUM,
     re.compile(r"\brsync\b[^|;&\n]*--delete\b"),
     "rsync --delete 目标端删除"),
    ("shell.nc-listen", SEV_MEDIUM,
     re.compile(r"\bnc(?:at)?\b[^|;&\n]*\s-l\b"),
     "netcat 监听端口"),
    # ---- low: supply chain / side-effect notices ----
    ("shell.package-install", SEV_LOW,
     re.compile(r"\b(?:pip3?|pipx|npm|yarn|pnpm|brew|apt(?:-get)?|yum|dnf|uv)"
                r"\b[^|;&\n]*\s(?:install|add|i)\b"),
     "安装外部包（供应链引入面）"),
]

# run_python: matched against the whole code block
_PYTHON_RULES: List[tuple] = [
    ("python.os-system", SEV_HIGH,
     re.compile(r"\bos\.(?:system|popen)\s*\("),
     "os.system / os.popen 直执行 shell"),
    ("python.eval-external", SEV_HIGH,
     re.compile(r"\b(?:eval|exec)\s*\(\s*(?:input|request|open|urlopen"
                r"|response|socket)"),
     "eval/exec 外部来源内容"),
    ("python.subprocess-shell", SEV_MEDIUM,
     re.compile(r"\bsubprocess\b[\s\S]{0,160}?shell\s*=\s*True"),
     "subprocess shell=True（经 shell 解释参数）"),
    ("python.rmtree", SEV_MEDIUM,
     re.compile(r"\bshutil\.rmtree\s*\(|\bos\.removedirs\s*\("),
     "递归删除目录树"),
    ("python.pty-spawn", SEV_MEDIUM,
     re.compile(r"\bpty\.spawn\s*\("),
     "pty.spawn 挂终端（交互劫持面）"),
    ("python.sensitive-path", SEV_MEDIUM,
     re.compile(r"id_rsa|id_ed25519|\.ssh[/\\]|\.env\b|\.netrc|\.pem\b"
                r"|\.aws[/\\]credentials|\.kube[/\\]config"),
     "代码触及密钥/凭据类路径"),
]

# write_text / edit_file: matched against the path arg (raw string and the
# expanduser-normalized form each get a try)
_WRITE_PATH_RULES: List[tuple] = [
    ("fs.write-ssh", SEV_HIGH,
     re.compile(r"(?:^|/)\.ssh/|(?:^|/)authorized_keys"),
     "写入 ~/.ssh / authorized_keys（登录持久化）"),
    ("fs.write-shell-rc", SEV_HIGH,
     re.compile(r"(?:^|/)\.(?:bashrc|zshrc|bash_profile|zprofile|zlogin"
                r"|profile)$"),
     "覆写 shell 启动脚本（登录即执行）"),
    ("fs.write-etc", SEV_HIGH,
     re.compile(r"^/etc/"),
     "写入 /etc 系统配置"),
    ("fs.write-credentials", SEV_HIGH,
     re.compile(r"id_rsa|id_ed25519|\.pem$|\.key$|\.netrc$"
                r"|\.aws[/\\]credentials|\.kube[/\\]config"
                r"|\.docker[/\\]config\.json"),
     "写入密钥/凭据存储文件"),
    ("fs.write-env", SEV_MEDIUM,
     re.compile(r"(?:^|/)\.env$"),
     "覆写 .env（密钥配置面）"),
    ("fs.write-gitconfig", SEV_MEDIUM,
     re.compile(r"(?:^|/)\.gitconfig$|\.config[/\\]git[/\\]config$"),
     "覆写 git 全局配置（可注入 url.insteadOf 劫持）"),
    ("fs.write-system-area", SEV_MEDIUM,
     re.compile(r"^/(?:System|Library|usr|bin|sbin|var)/"),
     "写入系统目录"),
    ("fs.path-traversal", SEV_LOW,
     re.compile(r"\.\./"),
     "相对路径含 .. 上跳（可能越出工作区）"),
]

# Tool-name groups (the guard only knows the field shapes in this list;
# MCP / custom tools are not scanned in v1 — whether to submit for review
# is decided by the LLM layer's suspicious signals)
_BASH_TOOLS = frozenset({"bash"})
_PY_TOOLS = frozenset({"run_python"})
_WRITE_TOOLS = frozenset({"write_text", "edit_file"})

_EVIDENCE_CHARS = 96


def _clip(text: str, limit: int = _EVIDENCE_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shell_segments(command: str) -> List[str]:
    """Split on ; / && / || / | / newline. A plain split, quote-unaware —
    separators inside quotes cause false positives, acceptable for the
    announce-only v1 (the conservative direction)."""
    return [seg.strip() for seg in _SHELL_SPLIT_RE.split(command) if seg.strip()]


def _match_rules(rules: List[tuple], texts: List[str]) -> List[tuple]:
    """Try each rule against the texts (whole string + segments), deduped by rule_id."""
    hits: "OrderedDict[str, tuple]" = OrderedDict()
    for rule_id, severity, pattern, summary in rules:
        for text in texts:
            m = pattern.search(text)
            if m:
                hits[rule_id] = (rule_id, severity, summary, _clip(m.group(0)))
                break
    return list(hits.values())


# Prefix commands allowed before the rm verb (sudo rm / xargs -0 rm / nohup rm…)
_RM_PREFIX_CMDS = frozenset({
    "sudo", "env", "nohup", "xargs", "command", "nice", "time", "timeout"})


def _rm_verb_index(tokens: List[str]) -> Optional[int]:
    """Locate the rm verb inside a segment: it can only appear after prefix
    commands like sudo/env/xargs — including the prefix's own flags AND the
    values those flags/prefixes consume (``sudo -u root rm``, ``timeout 10
    rm``, ``nice -n 5 rm``, ``env FOO=bar rm``). A bare token with no
    flag/prefix/value-slot immediately before it still ends the scan (avoids
    false positives like ``grep rm -r -f log`` treating a search term as the
    verb)."""
    prev_takes_value = False
    for i, tok in enumerate(tokens):
        name = os.path.basename(tok)
        if name == "rm":
            return i
        if tok.startswith("-") and len(tok) > 1:
            prev_takes_value = True     # flag: may consume the immediately following value (-u root / -n 5)
            continue
        if name in _RM_PREFIX_CMDS:
            prev_takes_value = True     # prefix command: may carry a value directly (timeout 10)
            continue
        if prev_takes_value:
            if "=" in tok:
                continue                # env's VAR=VALUE chain (does not consume the flag slot)
            prev_takes_value = False    # consumes exactly one value token, only one
            continue
        return None
    return None


def _scan_rm_dangerous(command: str) -> Optional[tuple]:
    """Structured detection of rm's "recursive + force" combo (same id as
    _SHELL_RULES' shell.rm-recursive-force; the caller dedups by id).

    The regex only recognizes forms with r/f squeezed into one token and is
    blind to ``rm -r -f`` / ``rm --recursive --force`` / ``rm -Rf`` — here
    the option set is read via shlex tokenization, close to rm's own parse
    semantics (including trailing flags like ``rm dir -r -f``). Segments
    where shlex raises (unbalanced quotes) fall back to the _RM_RF_RE
    bottom line."""
    for seg in _shell_segments(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            m = _RM_RF_RE.search(seg)
            if m:
                return (_RM_RF_RULE_ID, SEV_HIGH, _RM_RF_SUMMARY,
                        _clip(m.group(0)))
            continue
        idx = _rm_verb_index(tokens)
        if idx is None:
            continue
        recursive = force = False
        for tok in tokens[idx + 1:]:
            if tok == "--":
                break
            if tok == "--recursive":
                recursive = True
            elif tok == "--force":
                force = True
            elif tok.startswith("-") and len(tok) > 1 and tok[1:].isalpha():
                letters = set(tok[1:].lower())
                if "r" in letters:
                    recursive = True
                if "f" in letters:
                    force = True
        if recursive and force:
            return (_RM_RF_RULE_ID, SEV_HIGH, _RM_RF_SUMMARY, _clip(seg))
    return None


def _scan_write_path(raw_path: str) -> List[tuple]:
    """Path rules: aligned with the guarded tools' resolution shapes, each
    candidate tried once.

    file_tool._resolve_path does strip().expanduser(); the kernel treats
    //etc as /etc when opening files; on macOS /etc → /private/etc is a
    symlink, so the resolve() path read_text echoes back feeds the model
    the /private spelling — all three spellings must hit. /ETC/.SSH-style
    variants on case-insensitive filesystems are covered by lowercased
    candidates (an observe-only channel; over-reporting beats missing).
    Symlink resolution needs IO, violating this layer's "no IO" contract —
    a known residual."""
    stripped = str(raw_path).strip()
    texts: List[str] = []
    seen = set()

    def _add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            texts.append(candidate)

    _add(stripped)
    try:
        _add(str(Path(stripped).expanduser()))
    except Exception:
        pass
    for base in list(texts):
        # Multi-slash normalization (normpath deliberately keeps a leading //, so it is not used)
        _add(re.sub(r"/{2,}", "/", base))
        # macOS /private prefix stripping (the real location of /etc, /tmp, etc.)
        if base.startswith("/private/"):
            _add(base[len("/private"):])
        _add(base.lower())
    return _match_rules(_WRITE_PATH_RULES, texts)


def scan_tool_call(tool_name: str, args: Dict[str, Any]) -> List[GuardFinding]:
    """Pure rule scan (synchronous, no IO, no side effects) — shared by tests and the hook."""
    if not isinstance(args, dict):
        return []

    if tool_name in _BASH_TOOLS:
        command = str(args.get("command") or "")
        if not command:
            return []
        hits = _match_rules(_SHELL_RULES, [command] + _shell_segments(command))
        rm = _scan_rm_dangerous(command)
        if rm is not None and not any(h[0] == _RM_RF_RULE_ID for h in hits):
            hits.append(rm)
    elif tool_name in _PY_TOOLS:
        code = str(args.get("code") or "")
        if not code:
            return []
        hits = _match_rules(_PYTHON_RULES, [code])
    elif tool_name in _WRITE_TOOLS:
        path = str(args.get("path") or "")
        if not path:
            return []
        hits = _scan_write_path(path)
    elif tool_name == "create_cron":
        return [GuardFinding(
            tool_name, "cron.persistence", SEV_LOW,
            "创建定时作业 = 持久化执行面（重启后仍触发）",
            _clip(json.dumps(args, ensure_ascii=False)),
            source="rule")]
    else:
        return []

    return [GuardFinding(tool_name, rid, sev, summ, ev, source="rule")
            for rid, sev, summ, ev in hits]


# ============================================================================
# LLM review layer: suspicious-signal detection + background single-thread analyzer
# ============================================================================

# Signals that trigger review: network/execution verbs, pipes, command
# substitution, stealth encoding (redirects excluded — too common; a lone
# redirect does not justify a model call)
_LLM_SIGNAL_SHELL_RE = re.compile(
    r"\b(?:curl|wget|nc|ncat|ssh|scp|rsync|base64|eval|printenv|chmod"
    r"|chown|sudo|crontab|launchctl|osascript|python3?|perl|ruby|node)\b"
    r"|\||\$\(|`")
_LLM_SIGNAL_PY_RE = re.compile(
    r"\bsubprocess\b|\bsocket\b|\brequests\b|\burllib\b|os\.system"
    r"|\beval\s*\(|\bexec\s*\(|__import__|\bctypes\b|\bpty\b")


def _should_ask_llm(tool_name: str, args: Dict[str, Any],
                    findings: List[GuardFinding]) -> bool:
    """Only "no high-risk rule hit but suspicious signals" calls are worth a small-model call."""
    if any(f.severity == SEV_HIGH for f in findings):
        return False  # rules already characterized the high risk; no need to ask again
    if tool_name in _BASH_TOOLS:
        return bool(_LLM_SIGNAL_SHELL_RE.search(str(args.get("command") or "")))
    if tool_name in _PY_TOOLS:
        return bool(_LLM_SIGNAL_PY_RE.search(str(args.get("code") or "")))
    return False


_JUDGE_SYSTEM_PROMPT = (
    "你是 Agent 工具调用的安全审查器。判断给定工具调用是否属于危险操作"
    "（数据破坏、提权、持久化、凭据外泄、供应链、不可逆变更等），只依据"
    "调用本身判断，不臆测上下文。严格输出一行 JSON，不要 markdown 代码块：\n"
    '{"risk": "high|medium|low|none", "reason": "不超过80字的中文理由"}\n'
    "分级：high=数据丢失/系统破坏/凭据外泄；medium=不可逆变更或越权范围；"
    "low=轻微副作用；none=常规安全操作。"
)

_VERDICT_RISKS = {SEV_HIGH, SEV_MEDIUM, SEV_LOW, "none"}


def _parse_verdict(text: str) -> Optional[Dict[str, str]]:
    """Lenient parse of the model output: JSON between the first/last braces; an illegal risk is dropped."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("risk") not in _VERDICT_RISKS:
        return None
    return {
        "risk": str(payload["risk"]),
        "reason": _clip(str(payload.get("reason") or ""), 120),
    }


async def _default_probe(tool_name: str, args: Dict[str, Any],
                         llm_overrides: Dict[str, Any],
                         input_cap: int) -> Optional[Dict[str, str]]:
    """Default review probe: llm_overrides (tool_guard.llm > ambient
    snapshot) layered over the global llm_default builds the provider for
    one small-model completion."""
    from nexus.settings import get_llm_config
    from nexus.llm.resolve import build_provider

    base = dict(get_llm_config())
    base.update(llm_overrides or {})
    provider = build_provider(base)
    arg_text = json.dumps(args, ensure_ascii=False, default=str)[:input_cap]
    result = await provider.achat_completion(
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user",
             "content": f"工具: {tool_name}\n参数: {arg_text}"},
        ],
        model=base.get("model"),
        temperature=0.0,
        max_tokens=int(base.get("max_tokens") or 256),
    )
    return _parse_verdict((result or {}).get("content") or "")


class _LLMAnalyzer:
    """Background single-thread analyzer: bounded queue, bounded dedup, one asyncio.run per item.

    The thread starts lazily on first submit (zero cost for processes
    without the guard); the probe is injectable (test double). Any failure —
    full queue, timeout, provider error, malformed output — only logs at
    debug and drops the item, never touching the main loop.
    """

    def __init__(self, probe: Optional[Callable] = None,
                 max_queue: int = 64, dedup_cap: int = 256):
        self._probe = probe or _default_probe
        self._queue: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._dedup_cap = dedup_cap
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

    # -- submit side (event-loop thread; must be non-blocking) -----------------------------

    def submit(self, tool_name: str, args: Dict[str, Any],
               llm_overrides: Optional[Dict[str, Any]],
               max_input_chars: int, timeout_seconds: float,
               session_id: str = "", node_code: str = "") -> bool:
        """Enqueue one call for review. Duplicate calls (same tool + same args) are swallowed;
        a full queue drops the new entry and logs debug — an observability
        bypass is never worth backpressuring the main loop."""
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False,
                         default=str)
        key = hashlib.sha1(
            f"{tool_name}\x00{raw}".encode("utf-8")).hexdigest()[:16]
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._dedup_cap:
            self._seen.popitem(last=False)
        try:
            self._queue.put_nowait(
                (tool_name, dict(args or {}), dict(llm_overrides or {}),
                 int(max_input_chars), float(timeout_seconds),
                 session_id, node_code))
        except queue.Full:
            logger.debug("[tool_guard] LLM 判读队列已满，丢弃: %s", tool_name)
            return False
        self._ensure_thread()
        return True

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, name="tool-guard-llm", daemon=True)
                self._thread.start()

    # -- consume side (worker thread) -----------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                (tool_name, args, llm_overrides, cap, timeout,
                 session_id, node_code) = item
                verdict = asyncio.run(asyncio.wait_for(
                    self._probe(tool_name, args, llm_overrides, cap),
                    timeout=timeout))
                if verdict and verdict.get("risk") not in (None, "none"):
                    _record_finding(GuardFinding(
                        tool=tool_name, rule_id="llm.verdict",
                        severity=verdict["risk"],
                        summary=f"LLM 判读: {verdict.get('reason', '')}",
                        evidence=_clip(json.dumps(args, ensure_ascii=False,
                                                  default=str)),
                        session_id=session_id, node_code=node_code,
                        source="llm"))
            except Exception as e:  # any review failure stays silent (it is just an observability bypass)
                logger.debug("[tool_guard] LLM 判读失败（丢弃）: %s", e)
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Test helper: wait for the queue to drain (including an in-flight item)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def reset(self) -> None:
        """Clear the dedup table and pending queue (test isolation; leaves the thread alone)."""
        self._seen.clear()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break


# Lazy construction: the queue capacity is fixed from config at first use
# (queue.Queue's maxsize is set at construction; swapping the queue at
# runtime would strand the worker blocked on the old queue's get) — tests
# replace _ANALYZER via monkeypatch to inject their own instance
_ANALYZER: Optional[_LLMAnalyzer] = None
_ANALYZER_LOCK = threading.Lock()


def _get_analyzer(max_queue: int) -> _LLMAnalyzer:
    global _ANALYZER
    with _ANALYZER_LOCK:
        if _ANALYZER is None:
            _ANALYZER = _LLMAnalyzer(max_queue=max(1, int(max_queue)))
        return _ANALYZER


# ============================================================================
# Ledger / counters (in-memory, cleared on restart; consumed by recent_findings())
# ============================================================================

_LEDGER_CAP = 200
_LEDGER: "deque" = deque(maxlen=_LEDGER_CAP)
_LEDGER_LOCK = threading.Lock()
_STATS = {"findings": 0, "llm_asked": 0,
          "llm_verdicts": {"high": 0, "medium": 0, "low": 0}}


def _record_finding(finding: GuardFinding) -> None:
    with _LEDGER_LOCK:
        _LEDGER.append(finding)
        _STATS["findings"] += 1
        if finding.source == "llm":
            _STATS["llm_verdicts"][finding.severity] = \
                _STATS["llm_verdicts"].get(finding.severity, 0) + 1
    level = _SEV_LOG_LEVEL.get(finding.severity, logging.INFO)
    logger.log(
        level,
        "[tool_guard] %s %s 命中 %s（%s）— 证据: %r [session=%s node=%s]",
        _SEV_TAG.get(finding.severity, ""), finding.tool, finding.rule_id,
        finding.summary, finding.evidence, finding.session_id,
        finding.node_code,
    )


def recent_findings() -> List[GuardFinding]:
    """Snapshot of the latest hits (observability/testing; a read-only copy)."""
    with _LEDGER_LOCK:
        return list(_LEDGER)


def guard_stats() -> Dict[str, Any]:
    with _LEDGER_LOCK:
        return {
            "findings": _STATS["findings"],
            "llm_asked": _STATS["llm_asked"],
            "llm_verdicts": dict(_STATS["llm_verdicts"]),
        }


def reset_guard_state() -> None:
    """Clear the ledger / counters / dedup (test isolation)."""
    with _LEDGER_LOCK:
        _LEDGER.clear()
        _STATS.update(findings=0, llm_asked=0,
                      llm_verdicts={"high": 0, "medium": 0, "low": 0})
    if _ANALYZER is not None:
        _ANALYZER.reset()


# ============================================================================
# The hook itself (P4 on_tool_call)
# ============================================================================

# Fallback when the config read fails (conservative: rules on, LLM off — no network)
_FALLBACK_CONFIG = {
    "enabled": True, "llm_fallback": False,
    "llm_max_input_chars": 2000, "llm_max_queue": 64,
    "llm_timeout_seconds": 15.0, "llm": {},
}


def _load_guard_config() -> Dict[str, Any]:
    try:
        from nexus.settings import get_tool_guard_config
        return get_tool_guard_config()
    except Exception:
        return dict(_FALLBACK_CONFIG)


def _guard_on_tool_call(event) -> None:
    """P4 hook: scan → announce → submit suspicious calls for review. Always returns None (no rewrite)."""
    args = event.args
    if not isinstance(args, dict):
        return None
    cfg = _load_guard_config()
    if not cfg.get("enabled"):
        return None

    # Requirement: subagent / workflow runs are not scanned. P4 is
    # structurally off the sub-loop path; reading the flags here is a second
    # line of defense (against someone wiring P4 into another path later).
    tc = current_tool_context()
    if tc is not None and (tc.in_subagent or tc.in_workflow):
        return None

    sid = getattr(event, "session_id", "")
    node = getattr(event, "node_code", "")
    findings = scan_tool_call(event.tool_name, args)
    for f in findings:
        _record_finding(GuardFinding(
            tool=f.tool, rule_id=f.rule_id, severity=f.severity,
            summary=f.summary, evidence=f.evidence,
            session_id=sid, node_code=node, source=f.source))

    if (cfg.get("llm_fallback")
            and os.environ.get(LLM_DISABLED_ENV) != "1"
            and _should_ask_llm(event.tool_name, args, findings)):
        with _LEDGER_LOCK:
            _STATS["llm_asked"] += 1
        # ambient llm_config must be captured on the event-loop thread
        # (the contextvar does not cross threads); the tool_guard.llm
        # override has the higher priority, layered on top
        overrides: Dict[str, Any] = {}
        if tc is not None and tc.llm_config:
            overrides.update(tc.llm_config)
        overrides.update(cfg.get("llm") or {})
        _get_analyzer(int(cfg.get("llm_max_queue") or 64)).submit(
            event.tool_name, args, llm_overrides=overrides,
            max_input_chars=int(cfg.get("llm_max_input_chars") or 2000),
            timeout_seconds=float(cfg.get("llm_timeout_seconds") or 15.0),
            session_id=sid, node_code=node)
    return None


# ============================================================================
# Self-registration (AST scan requires a top-level module-body
# registry.register(...) expression)
# ============================================================================

def _build_tool_guard_hooks() -> Dict[str, List[Callable]]:
    """Zero-arg factory: plugin_registry.resolve calls it once and caches."""
    return {"on_tool_call": [_guard_on_tool_call]}


registry.register("agent_hooks", "tool_guard", _build_tool_guard_hooks)
