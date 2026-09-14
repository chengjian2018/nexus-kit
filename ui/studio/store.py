"""Studio 托管产物仓库 —— 固定目录 + 装载器（启动/reload 重放）。

两个固定托管目录（对齐 docs/design/ops-console-prd.md §7.1 D-1 的 studio 落点）：

- ``host/config/plugins/<stem>.py``    自动生成的插件模块。模块级注册习语与
  apps/*/route.py 相同（``plugin_registry.register("executor", code, Factory)``），
  首行注释约定 ``# studio-plugin: file=<stem>.py``（agent.py 解析用，装载不依赖）。
- ``host/config/patterns/<code>.yml``  控制台托管 pattern。同 code 后注册者生效，
  即 fork-to-edit 语义（console 版覆盖代码版）。

装载顺序固定「先插件后 pattern」：pattern 校验要解析插件 code，插件不先注册，
validate_plugin_declarations 会误报未注册。装载只做两类事：

1. 插件：以合成模块名（``studio_plugin_<stem>``）importlib 装载——每次都是全新
   module 对象重新 exec，模块级注册在 exec 中自然发生；重复装载产生的新类对象
   与旧注册同名不同物，必须开注册表 replace 窗口（对齐 host.reload._ReplaceMode）。
2. pattern：``pattern_from_yaml → validate_pattern → register`` 三段式（与 CLI
   ``pattern-load`` 同一条通路）。

失败语义：单文件失败只 ERROR 日志 + 收进 report（studio 列表可见），绝不阻断
服务——坏文件降级跳过，其余照常。安全边界：只装载这两个固定目录下的现存文件。

测试友好：目录常量是模块级 ``Path``，测试可按需 monkeypatch ``PLUGINS_DIR`` /
``PATTERNS_DIR``（tests/test_studio_api.py 即如此隔离仓库目录）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from nexus.model.serialization import pattern_from_yaml
from nexus.model.validation import validate_pattern
from nexus.registry.patterns import registry as pattern_registry
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "host" / "config" / "plugins"
PATTERNS_DIR = REPO_ROOT / "host" / "config" / "patterns"

# 托管文件名与 pattern code 的合法字符集（小写字母开头，小写字母/数字/下划线）
_FILENAME_STEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 最近一次装载的报告（studio 列表页展示坏文件用；进程内全局，无并发写竞争——
# 装载只发生在启动 / reload / apply 三个串行入口）
_last_report: Dict[str, Any] = {
    "plugins": {"loaded": [], "failed": {}},
    "patterns": {"loaded": [], "failed": {}},
}


# ---------------------------------------------------------------------------
# 目录视图
# ---------------------------------------------------------------------------

def plugin_files(plugins_dir: Optional[Path] = None) -> List[Path]:
    """托管插件文件（排序稳定，跳过 __init__ 等下划线开头文件）。"""
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.glob("*.py")
        if not p.name.startswith("_")
        and _FILENAME_STEM_RE.match(p.stem)
    )


def pattern_files(patterns_dir: Optional[Path] = None) -> List[Path]:
    """托管 pattern 文件（.yml/.yaml 均收，排序稳定）。"""
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    if not base.is_dir():
        return []
    return sorted(list(base.glob("*.yml")) + list(base.glob("*.yaml")))


def console_pattern_codes(patterns_dir: Optional[Path] = None) -> Set[str]:
    """已落盘的 console pattern codes（source 徽标判定依据）。"""
    return {p.stem for p in pattern_files(patterns_dir)}


def last_report() -> Dict[str, Any]:
    return _last_report


def pattern_load_error(code: str) -> Optional[str]:
    """某个 console pattern 上次装载的失败原因（None = 无记录/成功）。"""
    return _last_report["patterns"]["failed"].get(code)


# ---------------------------------------------------------------------------
# 写入 / 删除（apply / publish / fork 的落盘面；stem 白名单即安全边界）
# ---------------------------------------------------------------------------

def check_stem(stem: str) -> str:
    """校验托管文件名 stem（插件文件与 pattern code 共用一套合法字符集）。"""
    if not _FILENAME_STEM_RE.match(stem or ""):
        raise ValueError(
            f"非法文件名/code: {stem!r}（需小写字母开头，仅小写字母/数字/下划线，"
            f"长度 1-64）")
    return stem


def write_plugin_file(stem: str, code_text: str,
                      plugins_dir: Optional[Path] = None) -> Path:
    """写入（覆盖）一个托管插件模块文件。"""
    check_stem(stem)
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{stem}.py"
    path.write_text(code_text, encoding="utf-8")
    return path


def write_pattern_file(code: str, yaml_text: str,
                       patterns_dir: Optional[Path] = None) -> Path:
    """写入（覆盖）一个托管 pattern YAML 文件。"""
    check_stem(code)
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{code}.yml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def delete_pattern_file(code: str,
                        patterns_dir: Optional[Path] = None) -> bool:
    """删除托管 pattern 文件（不存在返回 False；多后缀逐一尝试）。"""
    check_stem(code)
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    removed = False
    for suffix in (".yml", ".yaml"):
        path = base / f"{code}{suffix}"
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def delete_plugin_file(stem: str,
                       plugins_dir: Optional[Path] = None) -> bool:
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    path = base / f"{stem}.py"
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# 插件模块装载
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _plugin_replace_window():
    """临时打开插件注册表的同名替换窗口（重新 exec 必然产生新类对象，
    严格模式会拒绝同名重注册；语义与 host.reload._ReplaceMode 一致）。"""
    old = plugin_registry.replace_on_conflict
    plugin_registry.replace_on_conflict = True
    try:
        yield
    finally:
        plugin_registry.replace_on_conflict = old


def plugin_module_name(stem: str) -> str:
    return f"studio_plugin_{stem}"


def import_plugin_module(path: Path, stem: Optional[str] = None) -> str:
    """以合成模块名 exec 一个插件模块文件，返回模块名。

    每次调用都构建全新 module 对象（覆盖 sys.modules 同名项）——重复装载 =
    重新 exec + 替换窗口内重注册，天然支持 reload 重放与 apply 后刷新。
    exec 失败时回收 sys.modules 占位并向上抛（调用方决定降级/中止）。
    """
    name = plugin_module_name(check_stem(stem if stem is not None else path.stem))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法为插件文件构建 import spec: {path}")
    module = importlib.util.module_from_spec(spec)
    with _plugin_replace_window():
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return name


def load_pattern_text(yaml_text: str):
    """三段式单文件装载：构造 → 校验 → 注册（与 CLI pattern-load 同款通路，
    但工具面宽松——托管目录里生成工作台应用的 pattern 可能引用后补注册的
    新工具，严格校验会让重启重放装载失败）。"""
    pattern = pattern_from_yaml(yaml_text)
    validate_pattern(pattern, strict_tools=False)
    pattern_registry.register(pattern)
    return pattern


# ---------------------------------------------------------------------------
# 全量装载（启动 / reload 重放入口）
# ---------------------------------------------------------------------------

def load_console_artifacts(plugins_dir: Optional[Path] = None,
                           patterns_dir: Optional[Path] = None,
                           ) -> Dict[str, Any]:
    """重放托管目录：先插件后 pattern；返回 report 并记入 _last_report。

    report 形如::

        {"plugins": {"loaded": ["a.py"], "failed": {"b.py": "<error>"}},
         "patterns": {"loaded": ["p1"], "failed": {"p2": "<error>"}}}
    """
    global _last_report
    report: Dict[str, Any] = {"plugins": {"loaded": [], "failed": {}},
                              "patterns": {"loaded": [], "failed": {}}}

    for path in plugin_files(plugins_dir):
        try:
            import_plugin_module(path)
            report["plugins"]["loaded"].append(path.name)
        except Exception as e:
            logger.exception("studio 插件装载失败，跳过: %s", path.name)
            report["plugins"]["failed"][path.name] = str(e)

    for path in pattern_files(patterns_dir):
        try:
            text = path.read_text(encoding="utf-8")
            pattern = load_pattern_text(text)
            report["patterns"]["loaded"].append(pattern.code)
        except Exception as e:
            logger.exception("studio pattern 装载失败，跳过: %s", path.name)
            report["patterns"]["failed"][path.stem] = str(e)

    _last_report = report
    loaded = (len(report["plugins"]["loaded"])
              + len(report["patterns"]["loaded"]))
    failed = (len(report["plugins"]["failed"])
              + len(report["patterns"]["failed"]))
    logger.info("studio 托管产物装载完成: 成功 %d，失败 %d", loaded, failed)
    return report
