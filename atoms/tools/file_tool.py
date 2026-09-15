"""read_text / write_text / edit_file / list_dir / search_files /
find_files（toolset: filesystem）.

本地文件系统的常用 agent 工具六件套，覆盖"找文件 → 看目录 → 读内容 →
检索 → 局部修改 → 落盘"的完整回路；与 shell 工具集
（atoms/tools/shell_tool.py）互补：结构化文件操作走这里（确定性、有
护栏、无 shell 注入面），任意命令才走 bash。改局部内容优先 edit_file
（精确替换、不误伤未提及部分），整文件生成/重写才用 write_text。

授权（deny-by-default 三层收口：注册 toolset → pattern.allow_toolset →
node.use_tools）::

    pattern:
      allow_toolset: [filesystem, knowledge]
    node:
      use_tools: [read_text, list_dir, search_files]   # 按需点名

护栏（config ``file_tool`` 节可调）：read_text 内容截断 max_read_chars
（默认 50000）；write_text 拒绝超过 max_write_chars（默认 200000）的
写入（防上下文里的超大内容写盘）；list_dir 条目截断 max_list_entries
（默认 500）；search_files 命中截断 max_matches（默认 100，args 只能
调小），并跳过隐藏目录 / .git / __pycache__ / node_modules 与超过
1MB 的单文件。

不可取消线程的收口（to_thread 工作线程无法中途取消，必须在护栏内
自行收手）：read_text 先 stat 再读——非普通文件（设备/FIFO）拒读，
超过按字符上限折算的字节预算（max_read_chars * 4 + 1024）拒绝整读；
search_files / find_files 的目录 walk 与逐行匹配受单调时钟预算
（20s）约束，超时提前停止并标记 stopped_early。truncated 只在确实
放弃了后续命中时为 true——恰好等于上限且遍历自然耗尽不算截断。

路径语义：相对路径相对服务启动目录解析；~ 展开；write_text 自动创建
父目录。无路径沙箱（本地个人 kit 定位，能力边界收在 pattern 授权层，
同 shell_tool 的取舍说明）。

命名说明：读写工具叫 read_text / write_text 而非 read_file /
write_file——registry 对跨工具集同名工具是硬拒绝（防影子），而
read_file 是 MCP 生态高频工具名（z.ai zread 服务器即带同名工具），
内置占用通用名会静默顶掉 MCP 版注册；read_text 也更贴行为（只处理
文本，二进制拒绝）。MCP 侧真实撞名由 server 的 tool_name_prefix 兜底。
"""

import fnmatch
import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, List

from nexus.engine.tool_context import ambient_pattern_code
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_file_tool_config

logger = logging.getLogger(__name__)

# search_files 跳过的目录名（隐藏目录 + 常见生成物目录）
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv",
                        "venv", ".tox", ".mypy_cache", ".pytest_cache"})
# search_files 单文件大小上限（超大文件多半是数据/二进制，跳过）
_MAX_SEARCH_FILE_BYTES = 1_000_000
# search_files 单行命中回填的行长上限（防 minified 巨行刷屏）
_MAX_MATCH_LINE_CHARS = 200
# read_text 默认/最大行数（行数之外再有 max_read_chars 兜底）
_DEFAULT_READ_LINES = 2000
_MAX_READ_LINES = 5000
# search_files / find_files 单次调用的时间预算：to_thread 线程不可取消，
# 巨树 walk 或慢正则必须在预算内自行收手，否则占死共享线程池
_WALK_TIME_BUDGET_SECONDS = 20.0
# regex 模式跳过的行长上限：灾难回溯的最坏耗时随输入规模增长，
# 残余的单行风险由此行长上限兜底
_MAX_REGEX_LINE_CHARS = 64 * 1024


def _resolve_path(raw: Any) -> Path:
    """参数路径 → 展开的 Path（不强制存在，由各 handler 自行校验）。"""
    return Path(str(raw).strip()).expanduser()


# ---------------------------------------------------------------------------
# read_text
# ---------------------------------------------------------------------------

READ_TEXT_SCHEMA = {
    "name": "read_text",
    "description": (
        "读取文本文件内容。返回从 offset 行开始的至多 limit 行"
        "（1 行 = 1 个换行分段；offset 从 0 记）。文件不存在/是目录/"
        "疑似二进制时报错；设备/FIFO 等非普通文件与超过字节预算的"
        "大文件直接拒绝（请改用 bash 的 sed/awk）。超长内容会被截断"
        "——需要后面的部分时带上更大的 offset 续读。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（~ 展开，相对路径基于服务启动目录）"},
            "offset": {"type": "integer", "description": "起始行号（可选，0 起，默认 0）"},
            "limit": {"type": "integer", "description": "本次最多读多少行（可选，默认 2000，上限 5000）"},
        },
        "required": ["path"],
    },
}


def _handle_read_text(args: Dict[str, Any]) -> str:
    raw_path = args.get("path")
    if raw_path is None or str(raw_path).strip() == "":
        return tool_error("path 必填：要读取的文件路径")
    path = _resolve_path(raw_path)
    if not path.exists():
        return tool_error(f"文件不存在: {path}")
    if path.is_dir():
        return tool_error(f"路径是目录不是文件: {path}（列目录请用 list_dir）")

    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return tool_error("offset 应为非负整数（0 起的行号）")
    try:
        limit = _DEFAULT_READ_LINES if args.get("limit") is None \
            else int(args["limit"])
    except (TypeError, ValueError):
        return tool_error("limit 应为正整数（行数）")
    if limit < 1:
        return tool_error("limit 必须 ≥ 1")
    limit = min(limit, _MAX_READ_LINES)

    # stat 不触发 open（FIFO/设备不阻塞），必须先验类型与体量再读：
    # read_bytes 对 /dev/zero、FIFO 会永久挂死，而 to_thread 线程不可取消
    try:
        st = path.stat()
    except OSError as e:
        return tool_error(f"无法读取文件状态: {e}")
    if not stat.S_ISREG(st.st_mode):
        return tool_error("不是普通文件（设备/FIFO 等），拒绝读取")
    guard = get_file_tool_config(ambient_pattern_code())
    cap = int(guard["max_read_chars"])
    # 字节预算按字符上限折算（UTF-8 单字符至多 4 字节）：超预算的文件
    # 整读后必然截断，白读不如直接拒绝
    byte_budget = cap * 4 + 1024
    if st.st_size > byte_budget:
        return tool_error(
            f"文件 {st.st_size} 字节超过读取上限 {byte_budget} 字节"
            f"（由 max_read_chars={cap} 折算）。请用 offset/limit 分页读取"
            "较小范围，或改用 bash 的 sed/awk 切片")

    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        return tool_error(f"疑似二进制文件，无法按文本读取: {path}")
    lines = data.decode("utf-8", errors="replace").splitlines()

    sliced = lines[offset:offset + limit]
    content = "\n".join(sliced)
    truncated = len(content) > cap
    if truncated:
        content = content[:cap] + \
            f"\n...[内容超长，已截断：共 {len(lines)} 行，本次保留 offset={offset} 起的前段]"
    return tool_result({
        "path": str(path.resolve()),
        "content": content,
        "total_lines": len(lines),
        "offset": offset,
        "lines_read": len(sliced),
        "truncated": truncated,
    })


# ---------------------------------------------------------------------------
# write_text
# ---------------------------------------------------------------------------

WRITE_TEXT_SCHEMA = {
    "name": "write_text",
    "description": (
        "把文本内容整体写入文件（全量覆盖语义：目标已有内容会被替换，"
        "不是追加；追加请自行先读后拼）。父目录不存在时自动创建。"
        "写入超过系统上限的内容会被拒绝——超大文件请分片多次写或用 "
        "run_python 生成。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径（~ 展开，相对路径基于服务启动目录）"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["path", "content"],
    },
}


def _handle_write_text(args: Dict[str, Any]) -> str:
    raw_path = args.get("path")
    if raw_path is None or str(raw_path).strip() == "":
        return tool_error("path 必填：目标文件路径")
    if args.get("content") is None:
        return tool_error("content 必填：要写入的完整文本（空内容请显式传空串）")
    content = str(args["content"])

    guard = get_file_tool_config(ambient_pattern_code())
    cap = int(guard["max_write_chars"])
    if len(content) > cap:
        return tool_error(
            f"内容 {len(content)} 字符超过写入上限 {cap}，已拒绝写入。"
            "超大文件请改用 run_python / bash 生成")

    path = _resolve_path(raw_path)
    if path.exists() and path.is_dir():
        return tool_error(f"目标路径是目录: {path}")
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("[write_text] %s %s (%d chars)",
                "覆盖" if existed else "新建", path, len(content))
    return tool_result({
        "path": str(path.resolve()),
        "created": not existed,
        "chars_written": len(content),
    })


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

LIST_DIR_SCHEMA = {
    "name": "list_dir",
    "description": (
        "列出目录下一层的条目（不递归）：名称、类型（dir/file/symlink）、"
        "大小。目录条目排前、名称排序；条目过多时截断。找深层文件用 "
        "search_files。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径（可选，默认服务启动目录）"},
        },
    },
}


def _entry_info(entry: Path) -> Dict[str, Any]:
    if entry.is_symlink():
        kind = "symlink"
    elif entry.is_dir():
        kind = "dir"
    else:
        kind = "file"
    size = 0
    if kind != "dir":
        try:
            size = entry.stat().st_size
        except OSError:
            pass
    return {"name": entry.name, "type": kind, "size": size}


def _handle_list_dir(args: Dict[str, Any]) -> str:
    raw = args.get("path")
    path = Path.cwd() if raw is None or str(raw).strip() == "" \
        else _resolve_path(raw)
    if not path.exists():
        return tool_error(f"目录不存在: {path}")
    if not path.is_dir():
        return tool_error(f"路径不是目录: {path}")

    try:
        entries = sorted(path.iterdir(),
                         key=lambda e: (0 if e.is_dir() else 1, e.name))
    except OSError as e:
        return tool_error(f"无法读取目录: {e}")

    guard = get_file_tool_config(ambient_pattern_code())
    cap = int(guard["max_list_entries"])
    total = len(entries)
    truncated = total > cap
    payload_entries = [_entry_info(e) for e in entries[:cap]]
    return tool_result({
        "path": str(path.resolve()),
        "entries": payload_entries,
        "total": total,
        "truncated": truncated,
    })


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": (
        "在目录下递归搜索文件内容：逐行匹配（子串或正则，可选忽略"
        "大小写），文件名需先命中 glob 模式（默认 *）。返回命中文件、"
        "行号与该行文本。自动跳过 .git / __pycache__ / node_modules 等"
        "目录、隐藏目录与超过 1MB 的文件；搜索超过时间预算会提前停止"
        "并标记 stopped_early（可缩小 path/glob 后重试）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索内容：regex=false 时为文本片段（子串匹配），regex=true 时为 Python 正则"},
            "path": {"type": "string", "description": "搜索根目录（可选，默认服务启动目录）"},
            "glob": {"type": "string", "description": "文件名过滤 glob（可选，默认 *；如 *.py）"},
            "regex": {"type": "boolean", "description": "把 query 当 Python 正则用 re.search 逐行匹配（可选，默认 false 子串匹配）"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写（可选，默认 false）"},
            "max_matches": {"type": "integer", "description": "命中条数上限（可选；只能调小）"},
        },
        "required": ["query"],
    },
}


def _iter_candidate_files(root: Path, pattern: str, deadline: float):
    """walk 根目录产出文件名命中 glob 的候选文件（跳过生成物目录）。

    超过 deadline 即停止产出（巨树兜底）；调用方负责感知超时并标记
    stopped_early。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            return
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if fnmatch.fnmatch(name, pattern):
                yield Path(dirpath) / name


def _handle_search_files(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "")
    if not query:
        return tool_error("query 必填：要搜索的文本片段")
    raw = args.get("path")
    root = Path.cwd() if raw is None or str(raw).strip() == "" \
        else _resolve_path(raw)
    if not root.is_dir():
        return tool_error(f"搜索根目录不存在或不是目录: {root}")
    pattern = str(args.get("glob") or "*")
    ignore_case = bool(args.get("ignore_case") or False)
    use_regex = bool(args.get("regex") or False)

    matcher = None
    if use_regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            matcher = re.compile(query, flags)
        except re.error as e:
            return tool_error(f"query 不是合法的 Python 正则: {e}")
    else:
        needle = query.lower() if ignore_case else query

    guard = get_file_tool_config(ambient_pattern_code())
    cap = int(guard["max_matches"])
    if args.get("max_matches") is not None:
        try:
            requested = int(args["max_matches"])
        except (TypeError, ValueError):
            return tool_error("max_matches 应为正整数")
        if requested < 1:
            return tool_error("max_matches 必须 ≥ 1")
        cap = min(requested, cap)

    matches: List[Dict[str, Any]] = []
    files_searched = 0
    truncated = False
    stopped_early = False
    started = time.monotonic()
    deadline = started + _WALK_TIME_BUDGET_SECONDS
    for file_path in _iter_candidate_files(root, pattern, deadline):
        if time.monotonic() > deadline:
            stopped_early = True
            break
        try:
            if file_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                continue
            data = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue  # 疑似二进制
        files_searched += 1
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if time.monotonic() > deadline:
                stopped_early = True
                break
            if matcher is not None and len(line) > _MAX_REGEX_LINE_CHARS:
                continue  # 超长行不进正则：限制回溯输入规模（见常量注释）
            if matcher is not None:
                hit = matcher.search(line) is not None
            else:
                hay = line.lower() if ignore_case else line
                hit = needle in hay
            if hit:
                if len(matches) >= cap:
                    # 满额后又见命中才标记截断——恰好 cap 条且 walk 自然
                    # 耗尽时 truncated 保持 false，避免误导后续补搜
                    truncated = True
                    break
                matches.append({
                    "path": str(file_path),
                    "line": line_no,
                    "text": line.strip()[:_MAX_MATCH_LINE_CHARS],
                })
        if truncated or stopped_early:
            break
    # walk 可能已在生成器内因超时先行停止（不再产出），此处兜底感知
    if not stopped_early and time.monotonic() > deadline:
        stopped_early = True

    return tool_result({
        "query": query,
        "root": str(root.resolve()),
        "glob": pattern,
        "regex": use_regex,
        "matches": matches,
        "truncated": truncated or stopped_early,
        "stopped_early": stopped_early,
        "files_searched": files_searched,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    })


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

EDIT_FILE_SCHEMA = {
    "name": "edit_file",
    "description": (
        "对文本文件做精确字符串替换编辑：old_str 必须与文件内容逐字符"
        "一致（含缩进与换行）。默认要求全文件唯一匹配；出现多次时要么"
        "扩大 old_str 上下文使其唯一，要么 replace_all=true 全部替换。"
        "new_str 传空串为删除。比整文件重写省 token 且不会误伤未提及的"
        "部分——改局部内容优先用它而不是 write_text。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "old_str": {"type": "string", "description": "被替换的原文（必须与文件内容精确一致，含空白）"},
            "new_str": {"type": "string", "description": "替换后的新文本（空串 = 删除 old_str）"},
            "replace_all": {"type": "boolean", "description": "old_str 出现多次时全部替换（可选，默认 false；false 下多处匹配会报错）"},
        },
        "required": ["path", "old_str", "new_str"],
    },
}


def _handle_edit_file(args: Dict[str, Any]) -> str:
    raw_path = args.get("path")
    if raw_path is None or str(raw_path).strip() == "":
        return tool_error("path 必填：目标文件路径")
    old_str = str(args.get("old_str"))
    new_str = str(args.get("new_str"))
    if not old_str:
        return tool_error("old_str 必填且非空（清空整个文件请用 write_text）")
    if old_str == new_str:
        return tool_error("old_str 与 new_str 相同，无意义的编辑")
    replace_all = bool(args.get("replace_all") or False)

    path = _resolve_path(raw_path)
    if not path.exists():
        return tool_error(f"文件不存在: {path}")
    if path.is_dir():
        return tool_error(f"路径是目录不是文件: {path}")

    guard = get_file_tool_config(ambient_pattern_code())
    size_cap = int(guard["max_edit_chars"])
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        return tool_error(f"疑似二进制文件，无法按文本编辑: {path}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        # 严格解码拒绝：errors=replace 的替换结果一旦回写，整个文件（而
        # 非仅编辑区）都会被 U+FFFD 固化——GBK 等编码文件就此不可逆损坏
        return tool_error(
            f"文件不是 UTF-8 文本（可能是 GBK 等其他编码），拒绝编辑以"
            f"避免整文件乱码回写。请先转码（如 iconv -f GBK -t UTF-8）"
            f"后再试: {path}")
    if len(content) > size_cap:
        return tool_error(
            f"文件 {len(content)} 字符超过编辑上限 {size_cap}；超大文件请用 "
            "bash 里的 sed/awk 或分片处理")
    count = content.count(old_str)
    if count == 0:
        return tool_error(
            "old_str 在文件中未找到（需与文件内容逐字符一致，注意缩进/"
            "空格/换行）。可先用 read_text 核对原文")
    if count > 1 and not replace_all:
        return tool_error(
            f"old_str 出现 {count} 次。请扩大上下文使其唯一，或传 "
            "replace_all=true 全部替换")

    path.write_text(content.replace(old_str, new_str), encoding="utf-8")
    logger.info("[edit_file] %s: %d 处替换", path, count if replace_all else 1)
    return tool_result({
        "path": str(path.resolve()),
        "replacements": count if replace_all else 1,
    })


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------

FIND_FILES_SCHEMA = {
    "name": "find_files",
    "description": (
        "按文件名 glob 模式递归查找文件（只找名字不搜内容；搜内容用 "
        "search_files）。pattern 按相对路径匹配：*.py 匹配任意深度的 "
        "Python 文件，docs/* 匹配 docs 下一层，data/** 匹配 data 下全部。"
        "自动跳过 .git / __pycache__ / node_modules 等目录与隐藏目录；"
        "遍历超过时间预算会提前停止并标记 stopped_early。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "文件名 glob 模式，如 *.py、test_*.py、docs/**"},
            "path": {"type": "string", "description": "查找根目录（可选，默认服务启动目录）"},
            "max_results": {"type": "integer", "description": "返回条数上限（可选；只能调小）"},
        },
        "required": ["pattern"],
    },
}


def _handle_find_files(args: Dict[str, Any]) -> str:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return tool_error("pattern 必填：文件名 glob 模式（如 *.py）")
    raw = args.get("path")
    root = Path.cwd() if raw is None or str(raw).strip() == "" \
        else _resolve_path(raw)
    if not root.is_dir():
        return tool_error(f"查找根目录不存在或不是目录: {root}")

    guard = get_file_tool_config(ambient_pattern_code())
    cap = int(guard["max_find_results"])
    if args.get("max_results") is not None:
        try:
            requested = int(args["max_results"])
        except (TypeError, ValueError):
            return tool_error("max_results 应为正整数")
        if requested < 1:
            return tool_error("max_results 必须 ≥ 1")
        cap = min(requested, cap)

    # pattern 匹配相对 posix 路径（fnmatch 的 * 天然跨目录分隔符，
    # 所以 *.py 即任意深度）；walk 侧已保证跳过生成物目录
    results: List[str] = []
    truncated = False
    stopped_early = False
    started = time.monotonic()
    deadline = started + _WALK_TIME_BUDGET_SECONDS
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            stopped_early = True
            break
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if time.monotonic() > deadline:
                stopped_early = True
                break
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                if len(results) >= cap:
                    # 满额后又见候选才标记截断——恰好 cap 条且 walk 自然
                    # 耗尽时 truncated 保持 false，避免误导后续补找
                    truncated = True
                    break
                results.append(str(Path(dirpath) / name))
        if truncated or stopped_early:
            break

    return tool_result({
        "pattern": pattern,
        "root": str(root.resolve()),
        "files": results,
        "total": len(results),
        "truncated": truncated or stopped_early,
        "stopped_early": stopped_early,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    })


# ---------------------------------------------------------------------------
# Self-registration (registered on module import; AST scan auto-discovery —
# 注意必须是顶层 registry.register() 调用表达式：扫描器（nexus/registry/
# discovery.py）只匹配 module body 的 Expr，for 循环体内的调用不可见)
# ---------------------------------------------------------------------------

registry.register(
    name="read_text",
    toolset="filesystem",
    schema=READ_TEXT_SCHEMA,
    handler=_handle_read_text,
    description="读文本文件（offset/limit 分页，截断护栏）",
    emoji="📖",
)

registry.register(
    name="write_text",
    toolset="filesystem",
    schema=WRITE_TEXT_SCHEMA,
    handler=_handle_write_text,
    description="全量写文件（自动建父目录，超限拒绝）",
    emoji="✍️",
)

registry.register(
    name="list_dir",
    toolset="filesystem",
    schema=LIST_DIR_SCHEMA,
    handler=_handle_list_dir,
    description="列目录一层（名称/类型/大小）",
    emoji="📂",
)

registry.register(
    name="search_files",
    toolset="filesystem",
    schema=SEARCH_FILES_SCHEMA,
    handler=_handle_search_files,
    description="递归内容搜索（子串/正则匹配+glob 过滤）",
    emoji="🔎",
)

registry.register(
    name="edit_file",
    toolset="filesystem",
    schema=EDIT_FILE_SCHEMA,
    handler=_handle_edit_file,
    description="精确字符串替换编辑（唯一匹配或 replace_all）",
    emoji="✂️",
)

registry.register(
    name="find_files",
    toolset="filesystem",
    schema=FIND_FILES_SCHEMA,
    handler=_handle_find_files,
    description="按文件名 glob 递归查找文件",
    emoji="🗂️",
)
