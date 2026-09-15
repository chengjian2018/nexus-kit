"""tool_guard —— P4 工具执行前危险操作播报（kind="agent_hooks"）.

借鉴 Claude Code 的 bash 权限分析（命令分段 + 规则命中 + 次级小模型
判读）与 hermes 的 declarative guardrail 风格，做成 agent_hooks 插件包：

- **规则层（同步、微秒级）**：编译好的正则规则表按工具扫描
  ``bash`` 的 command（先整串、再按 ``; / && / || / | / 换行`` 分段，
  使管道规则与段子规则都能命中）、``run_python`` 的 code、
  ``write_text`` / ``edit_file`` 的 path、``create_cron``。
- **LLM 判读层（异步、旁路）**：规则未命中高危但命令带可疑信号
  （网络动词 / 管道 / 命令替换 / 隐匿编码）时，把调用提交给后台单线程
  分析器，用轻量模型输出 ``{"risk": ..., "reason": ...}`` JSON 裁决。
  分析绝不阻塞主循环：hook 在事件循环线程里只做入队，LLM 调用在
  worker 线程内 ``asyncio.run`` 独立执行，队列满即丢弃新条。

v1 契约（对应"先播报、不卡控"）：

- hook 恒返回 ``None``（P4 语义 = 不改写 name/args）；
- subagent / workflow 运行期不校验——P4 本就不在 ``_run_sub_agent``
  的路径上（子循环直连 registry.dispatch），此处再读一次
  ``current_tool_context()`` 标志位做双保险；
- hook 异常由 agent_hooks 分发器统一吞掉，本模块内部也各自兜底
  （配置读不到 → 默认值；LLM 失败 → 静默丢弃），播报通道只有日志
  与内存 ledger（``recent_findings()``，供 studio/测试取用）。

启用方式（pattern 级声明，与 allow_toolset 授权哲学一致——谁授了
shell/filesystem 谁挂 guard）::

    Pattern(..., plugins={"agent_hooks": "tool_guard"}, ...)

配置节 ``tool_guard``（可选，见 local_config.example.yaml）：
enabled / llm_fallback / llm_max_input_chars / llm_max_queue /
llm_timeout_seconds / llm（llm 为叠加在 ambient 连接之上的 judge 模型
覆盖，推荐指向便宜小模型）。
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

# 测试隔离开关（conftest 置 1）：杀掉 LLM 判读的旁路线程路径
LLM_DISABLED_ENV = "NEXUS_TOOL_GUARD_LLM_DISABLED"

# ============================================================================
# 裁决与播报的数据形状
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
    """一次命中（规则或 LLM 裁决同形，方便 ledger / UI 统一消费）。"""

    tool: str
    rule_id: str          # 如 "shell.rm-force" / "llm.verdict"
    severity: str         # high | medium | low
    summary: str          # 一句话危险定性
    evidence: str         # 命中片段（已截断）
    session_id: str = ""
    node_code: str = ""
    source: str = "rule"  # rule | llm


# ============================================================================
# 规则表（正则均为模块加载期一次性编译）
# ============================================================================

# --- bash：分段符。注意 "curl … | sh" 这类管道规则必须在整串上匹配，
# --- 而 "rm -rf" 这类段子规则在分段后匹配——两张都跑，按 rule_id 去重。
# --- rm 的递归+强制另走 _scan_rm_dangerous 结构化检测（正则对拆分旗标/
# --- 长旗标/大写 -R 全部失明），同样按 rule_id 去重。
_SHELL_SPLIT_RE = re.compile(r"\n|&&|\|\||;|\|")

_RM_RF_RULE_ID = "shell.rm-recursive-force"
_RM_RF_SUMMARY = "递归强制删除（rm -rf 族，误伤不可逆）"
# 同一 token 内挤着 r 和 f 的组合旗标形态（-rf/-fr/-Rf/-rvf…）——结构化
# 检测的 shlex 失败（引号不平衡）时的兜底层，正常路径先由它快速命中
_RM_RF_RE = re.compile(r"\brm\b[^|;&\n]*\s-[a-zA-Z]*r[a-zA-Z]*f\b"
                       r"|\brm\b[^|;&\n]*\s-[a-zA-Z]*f[a-zA-Z]*r\b")

# (rule_id, severity, pattern, summary) —— 先整串后分段各试一次
_SHELL_RULES: List[tuple] = [
    # ---- 高：破坏 / 提权执行面 / 远端历史改写 ----
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
    # ---- 中：不可逆 / 持久化 / 越权面 ----
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
    # ---- 低：供应链 / 副作用提示 ----
    ("shell.package-install", SEV_LOW,
     re.compile(r"\b(?:pip3?|pipx|npm|yarn|pnpm|brew|apt(?:-get)?|yum|dnf|uv)"
                r"\b[^|;&\n]*\s(?:install|add|i)\b"),
     "安装外部包（供应链引入面）"),
]

# run_python：对整段 code 匹配
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

# write_text / edit_file：对 path 参数（原串 + expanduser 归一后各试一次）
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

# 工具名分组（guard 只认清单内的字段形状；MCP/自定义工具 v1 不扫，
# 交给 LLM 判读层的可疑信号决定是否送审）
_BASH_TOOLS = frozenset({"bash"})
_PY_TOOLS = frozenset({"run_python"})
_WRITE_TOOLS = frozenset({"write_text", "edit_file"})

_EVIDENCE_CHARS = 96


def _clip(text: str, limit: int = _EVIDENCE_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shell_segments(command: str) -> List[str]:
    """按 ; / && / || / | / 换行切段。简单 split，不感知引号——引号内的
    分隔符会造成误报，对"只播报不干预"的 v1 可接受（保守方向）。"""
    return [seg.strip() for seg in _SHELL_SPLIT_RE.split(command) if seg.strip()]


def _match_rules(rules: List[tuple], texts: List[str]) -> List[tuple]:
    """对 texts（整串 + 各段）逐一试规则，按 rule_id 去重后返回命中。"""
    hits: "OrderedDict[str, tuple]" = OrderedDict()
    for rule_id, severity, pattern, summary in rules:
        for text in texts:
            m = pattern.search(text)
            if m:
                hits[rule_id] = (rule_id, severity, summary, _clip(m.group(0)))
                break
    return list(hits.values())


# rm 动词之前允许出现的前缀命令（sudo rm / xargs -0 rm / nohup rm…）
_RM_PREFIX_CMDS = frozenset({
    "sudo", "env", "nohup", "xargs", "command", "nice", "time", "timeout"})


def _rm_verb_index(tokens: List[str]) -> Optional[int]:
    """定位段内 rm 动词位置：sudo/env/xargs 等前缀命令（及其自身旗标）
    之后才可能是动词；遇到其他非旗标 token 即该段动词不是 rm（避免
    ``grep rm -r -f log`` 这类把搜索词当动词的误报）。"""
    for i, tok in enumerate(tokens):
        name = os.path.basename(tok)
        if name == "rm":
            return i
        if name not in _RM_PREFIX_CMDS and not (i > 0 and tok.startswith("-")):
            return None
    return None


def _scan_rm_dangerous(command: str) -> Optional[tuple]:
    """结构化检测 rm 的「递归 + 强制」组合（与 _SHELL_RULES 的
    shell.rm-recursive-force 同 id，调用方按 id 去重）。

    正则只认 r/f 挤在同一个 token 的形态，对 ``rm -r -f`` /
    ``rm --recursive --force`` / ``rm -Rf`` 全部失明——这里按 shlex 分词
    读选项集合，贴近 rm 自身的解析语义（含 ``rm dir -r -f`` 旗标后置）。
    shlex 抛错（引号不平衡）的段退回 _RM_RF_RE 兜底。"""
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
    """路径规则：与被守工具的解析形态对齐后多候选各试一次。

    file_tool._resolve_path 做 strip().expanduser()；内核开档把 //etc
    当 /etc；macOS 上 /etc → /private/etc 是 symlink，read_text 回显的
    resolve() 路径会以 /private 拼法喂给模型——三个拼法都要能命中。
    大小写不敏感文件系统上的 /ETC/.SSH 变体用小写化候选覆盖（observe-
    only 通道，多报不错过可接受）。symlink 解析需要 IO，违背本层
    「无 IO」契约，是已知残留。"""
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
        # 多斜杠归一（normpath 特意保留前导 //，不适用）
        _add(re.sub(r"/{2,}", "/", base))
        # macOS /private 前缀剥离（/etc、/tmp 等的真实落点）
        if base.startswith("/private/"):
            _add(base[len("/private"):])
        _add(base.lower())
    return _match_rules(_WRITE_PATH_RULES, texts)


def scan_tool_call(tool_name: str, args: Dict[str, Any]) -> List[GuardFinding]:
    """纯规则扫描（同步、无 IO、无副作用）——测试与 hook 共用。"""
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
# LLM 判读层：可疑信号识别 + 后台单线程分析器
# ============================================================================

# 触发判读的信号：网络/执行动词、管道、命令替换、隐匿编码（重定向不在
# 内——太常见，单独重定向不足以动用一次模型调用）
_LLM_SIGNAL_SHELL_RE = re.compile(
    r"\b(?:curl|wget|nc|ncat|ssh|scp|rsync|base64|eval|printenv|chmod"
    r"|chown|sudo|crontab|launchctl|osascript|python3?|perl|ruby|node)\b"
    r"|\||\$\(|`")
_LLM_SIGNAL_PY_RE = re.compile(
    r"\bsubprocess\b|\bsocket\b|\brequests\b|\burllib\b|os\.system"
    r"|\beval\s*\(|\bexec\s*\(|__import__|\bctypes\b|\bpty\b")


def _should_ask_llm(tool_name: str, args: Dict[str, Any],
                    findings: List[GuardFinding]) -> bool:
    """只有"规则没打出高危、但信号可疑"的调用才值得花一次小模型。"""
    if any(f.severity == SEV_HIGH for f in findings):
        return False  # 高危已由规则定性，不需要再问
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
    """宽松解析模型输出：截取首尾大括号间的 JSON；risk 非法则丢弃。"""
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
    """默认判读探针：llm_overrides（tool_guard.llm > ambient 快照）叠加在
    全局 llm_default 之上构造 provider，做一次小模型补全。"""
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
    """后台单线程分析器：queue 有界、去重有界、逐条 asyncio.run。

    线程在首次 submit 时惰性启动（不挂 guard 的进程零成本）；probe 可
    注入（测试替身）。任何失败——队列满、超时、provider 异常、输出
    不合法——都只记 debug 日志然后丢条，绝不影响主循环。
    """

    def __init__(self, probe: Optional[Callable] = None,
                 max_queue: int = 64, dedup_cap: int = 256):
        self._probe = probe or _default_probe
        self._queue: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._dedup_cap = dedup_cap
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

    # -- 提交端（事件循环线程，必须非阻塞） -----------------------------

    def submit(self, tool_name: str, args: Dict[str, Any],
               llm_overrides: Optional[Dict[str, Any]],
               max_input_chars: int, timeout_seconds: float,
               session_id: str = "", node_code: str = "") -> bool:
        """入队一条待审调用。重复调用（同 tool + 同 args）直接吞掉；
        队列满丢弃新条并记 debug——观测旁路不值得反压主循环。"""
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

    # -- 消费端（worker 线程） -----------------------------------------

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
            except Exception as e:  # 任何判读失败都静默（旁路观测而已）
                logger.debug("[tool_guard] LLM 判读失败（丢弃）: %s", e)
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """测试辅助：等队列清空（含正在处理的一条）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def reset(self) -> None:
        """清去重表与待处理队列（测试隔离用；不动线程）。"""
        self._seen.clear()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break


# 惰性构造：队列容量在首次使用时按配置定型（queue.Queue 的 maxsize
# 构造即定，运行期换队列会漏掉阻塞在旧队列 get 上的 worker）——测试
# 经 monkeypatch 替换 _ANALYZER 注入自己的实例
_ANALYZER: Optional[_LLMAnalyzer] = None
_ANALYZER_LOCK = threading.Lock()


def _get_analyzer(max_queue: int) -> _LLMAnalyzer:
    global _ANALYZER
    with _ANALYZER_LOCK:
        if _ANALYZER is None:
            _ANALYZER = _LLMAnalyzer(max_queue=max(1, int(max_queue)))
        return _ANALYZER


# ============================================================================
# Ledger / 计数（内存态，重启清空；供 recent_findings() 消费）
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
    """最近命中的快照（观测/测试用；只读副本）。"""
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
    """清空 ledger/计数/去重（测试隔离用）。"""
    with _LEDGER_LOCK:
        _LEDGER.clear()
        _STATS.update(findings=0, llm_asked=0,
                      llm_verdicts={"high": 0, "medium": 0, "low": 0})
    if _ANALYZER is not None:
        _ANALYZER.reset()


# ============================================================================
# Hook 本体（P4 on_tool_call）
# ============================================================================

# 配置读失败时的回退（保守：规则开、LLM 关——不动网络）
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
    """P4 hook：扫描 → 播报 → 视可疑度旁路送审。恒返回 None（不改写）。"""
    args = event.args
    if not isinstance(args, dict):
        return None
    cfg = _load_guard_config()
    if not cfg.get("enabled"):
        return None

    # 需求：subagent / workflow 运行期不校验。P4 结构上不在子循环路径，
    # 这里读标志位是双保险（防未来有人把 P4 接进别的路径）。
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
        # ambient llm_config 必须在事件循环线程里捕获（contextvar
        # 不跨线程）；tool_guard.llm 覆盖优先级更高，叠加其后
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
# 自注册（AST 扫描要求：模块体顶层的 registry.register(...) 表达式）
# ============================================================================

def _build_tool_guard_hooks() -> Dict[str, List[Callable]]:
    """零参工厂：plugin_registry.resolve 只调一次并缓存。"""
    return {"on_tool_call": [_guard_on_tool_call]}


registry.register("agent_hooks", "tool_guard", _build_tool_guard_hooks)
