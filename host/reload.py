"""热重载器 — llm config / pattern / plugin / channel 的运行时重载。

两类重载，机制完全不同：

**llm config（yaml 数据）**：``nexus.settings.load_config`` 自带
(mtime_ns, size) 指纹缓存——每轮 R1 刷新只 stat 不读文件，文件变了才
重新解析。因此本模块对 config 只做 ``invalidate_config_cache()``（防御
时钟回拨等指纹失效场景），不做主动重读。

**pattern / plugin / channel（python 代码）**：基于 mtime 的 re-import。
各注册表的注册发生在模块 import 时（AST 发现 → import → 模块级
``registry.register()``），所以"重载"= 重新执行注册：

1. 追踪扫描根（``apps/*/``、``atoms/executors/``）下**已 import** 的全部
   模块——不限于注册模块：prompts 等纯数据模块的变更要经"重放消费者"
   生效，不追踪就永远看不到；
2. stat 对比 mtime，找出变更模块；
3. 任一模块变更 → 全量按序重放（见下方排序说明）；
4. 注册表以 replace 模式收编新对象（见下方各注册表说明）。

**排序 = AST import 边的拓扑序**：``importlib.reload`` 只重执行目标模
块本身，不级联其依赖——若先 reload 消费者（route.py），它 import 的
prompts 还是旧对象。注意**不能**用 sys.modules 插入序：import 机制在
模块 body 执行前就插入 sys.modules，消费者反而排在依赖前面。这里解析
每个模块源码的 import 语句（含相对 import）建依赖边，分层拓扑排序，
循环依赖退化为原序补尾（顺序只是重放启发式，不构成正确性边界）。
不做 AST 注册谓词过滤：已 import 的模块在 import 时已通过注册检查，
当前磁盘版本可能是编辑中途的语法错文件——按谓词丢弃恰恰丢失了最需
要重载反馈的变更（重放失败 → 告警 + 保持旧注册）。

**全量重放**（变更 ∪ 未变注册模块 ∪ 未变非注册模块）：消费者只有自己
重执行才能绑定被依赖者刷新后的新对象（re-import 生成的新类/新常量）；
重放未变模块是幂等的再执行，代价可忽略。

**各注册表的重载语义**：

- pattern（``nexus.registry.patterns``）：``register`` 本就覆盖同名（重
  载=新 Pattern 换旧）。**运行中会话不受影响**——``session.pattern``
  持有旧对象引用，按旧拓扑跑完；新会话拿到新对象。会话重绑（切到新
  pattern）由宿主调用 :func:`rebind_sessions` 完成。
- plugin（``nexus.registry.plugins``）：默认"同 factory 幂等 / 不同
  factory 拒绝"。重载后类对象必然不同（re-import 生成新类），必须临
  时切 ``replace_on_conflict`` 模式替换条目并清实例缓存——正在执行的
  轮次持有旧 executor 引用继续跑完，新解析走新类。
- channel（``nexus.registry.channels``）：默认拒绝同名重复注册，同样
  走 replace 模式。**router 不重建**：``nexus.channels.webhooks`` 的
  handler 每请求从 registry 活取 spec，replace 后下一个请求即生效
  （token / default pattern 本就每请求读 env）。

**不在范围**：tools / MCP / providers。ToolRegistry 注册发生在 import
期且 handler 闭包绑定连接对象，re-import 会注册出双份工具；MCP 连接
有专门生命周期（``McpManager.arefresh``）；providers 是连接配置而非
业务代码。需要时重启进程。

宿主挂点：``host/main.py`` 的 ``POST /api/v1/reload`` 端点（startup 时
经 :func:`init_baseline` 建立基线）；CLI 的 ``/reload`` slash 命令；
``NEXUS_RELOAD_WATCH=1`` 时的 :class:`ReloadWatcher` 后台轮询。
"""

import ast
import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _discover_module_names() -> List[str]:
    """收集扫描域内已 import 的模块，按 sys.modules 插入序返回。

    扫描域 = 模块名前缀：``apps.<pkg>.<mod>``（更深嵌套也算，prompts 等
    非注册支撑模块一并追踪）与 ``atoms.executors.<mod>``。不扫 nexus/
    （内核不重载）与 atoms/stages（stage 实例被 pattern.stages 声明引
    用，重载需要级联重建 pattern，需要时重启进程）。进程内从未 import
    的文件不算变更——首次加载属于正常 discovery，热重载只管存量。
    """
    names: List[str] = []
    for name in list(sys.modules):  # dict 插入序 = import 完成序
        parts = name.split(".")
        if len(parts) >= 3 and parts[0] == "apps":
            names.append(name)
        elif len(parts) >= 3 and parts[0] == "atoms" and parts[1] == "executors":
            names.append(name)
    return names


def _module_path(name: str) -> Optional[Path]:
    """模块名 → 源文件路径；无源文件的（内置/命名空间包）返回 None。"""
    mod = sys.modules.get(name)
    origin = getattr(mod, "__file__", None)
    return Path(origin) if origin else None


# 模块 mtime 基线（init_baseline / 首次 reload_changed 建立；reload 成功
# 后刷新，失败的下次重试）
_MODULE_MTIMES: Dict[str, float] = {}


def _changed_modules(tracked: List[str]) -> List[str]:
    """mtime 对比找出变更模块（晚于基线记录值）。"""
    changed: List[str] = []
    for name in tracked:
        path = _module_path(name)
        if path is None or not path.exists():
            continue
        mtime = path.stat().st_mtime
        last = _MODULE_MTIMES.get(name)
        if last is None:
            _MODULE_MTIMES[name] = mtime  # 首次见到：建立基线
        elif mtime > last:
            changed.append(name)
    return changed


def _reload_module(name: str) -> bool:
    """re-import 单个模块；失败告警返回 False（不中断其余重放）。"""
    try:
        importlib.reload(sys.modules[name])
        return True
    except Exception as e:  # noqa: BLE001 -- 单模块失败不拖垮整体
        logger.warning("[reload] 重载 %s 失败（保持旧注册）: %s", name, e)
        return False


# ============================================================================
# 依赖序重放
# ============================================================================

def _module_imports(name: str, tracked: Set[str]) -> Set[str]:
    """模块源码 import 语句命中的、落在追踪集内的依赖（含相对 import 解析）。

    语法错 / 读失败返回空集——该模块视作无依赖（拓扑序里排最前；它自己
    的重放成败与顺序无关）。
    """
    path = _module_path(name)
    if path is None:
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()

    parts = name.split(".")
    candidates: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    candidates.add(node.module)
                    # from <pkg> import <mod> 形式：<mod> 是子模块
                    candidates.update(
                        f"{node.module}.{a.name}" for a in node.names)
            elif node.level <= len(parts) - 1:
                # 相对 import：level=1 从本模块所在包起算
                base = ".".join(parts[:-node.level])
                if node.module:
                    prefix = f"{base}.{node.module}" if base else node.module
                    candidates.add(prefix)
                    candidates.update(f"{prefix}.{a.name}" for a in node.names)
                else:
                    candidates.update(f"{base}.{a.name}" for a in node.names)
    return candidates & tracked


def _replay_order(tracked: List[str]) -> List[str]:
    """拓扑排序重放序（依赖先于消费者），discovery 序做稳定基准。

    **不能直接用 sys.modules 插入序**：import 机制在模块 body 执行*前*
    就把它插入 sys.modules——消费者先于其 body import 的依赖插入
    （route 先于 prompts），按插入序重放恰好反了。这里按 AST import 边
    做分层 Kahn（每轮放下依赖已齐的模块，轮内保持 discovery 序）；
    循环依赖退化为按原序补尾——顺序只是重放启发式，不构成正确性边界。
    """
    tracked_set = set(tracked)
    deps = {name: _module_imports(name, tracked_set) for name in tracked}

    ordered: List[str] = []
    placed: Set[str] = set()
    pending = list(tracked)
    while pending:
        ready = [n for n in pending if deps[n] <= placed]
        if not ready:
            ordered += pending  # 循环依赖：剩余按原序放掉
            break
        ordered += ready
        placed.update(ready)
        pending = [n for n in pending if n not in placed]
    return ordered


# ============================================================================
# 注册表 replace 模式
# ============================================================================

class _ReplaceMode:
    """临时打开注册表的同名替换（context manager）。

    重载产生的类/对象与旧的不同一（re-import 生成新类），而 plugin /
    channel 注册表默认"同名不同物 → 拒绝"。在重放窗口内切 replace，
    窗口结束恢复默认严格模式。
    """

    def __init__(self):
        from nexus.registry.channels import registry as channel_registry
        from nexus.registry.plugins import registry as plugin_registry
        self._plugin_reg = plugin_registry
        self._channel_reg = channel_registry

    def __enter__(self):
        self._plugin_reg.replace_on_conflict = True
        self._channel_reg.replace_on_conflict = True
        return self

    def __exit__(self, *exc):
        self._plugin_reg.replace_on_conflict = False
        self._channel_reg.replace_on_conflict = False
        return False


# ============================================================================
# 对外入口
# ============================================================================

def init_baseline() -> None:
    """建立全部追踪模块的 mtime 基线（幂等刷新）。

    host startup 调用——使首个 ``/api/v1/reload`` 即可检测开机以来的
    变更（否则首跑只建基线不重载）。watcher 启动时同样调用。
    """
    _changed_modules(_discover_module_names())


def reload_changed() -> Dict[str, List[str]]:
    """检测并重载变更的 pattern / plugin / channel 模块。

    Returns:
        {"changed": [...], "reloaded": [...], "failed": [...]} — changed 为
        mtime 检测出的变更模块（含 prompts 等非注册模块）；reloaded 为
        重放成功的（含连带重放的未变模块）；failed 为失败的（保持旧注
        册，下次 reload 重试）。
    """
    tracked = _discover_module_names()
    changed = _changed_modules(tracked)
    if not changed:
        return {"changed": [], "reloaded": [], "failed": []}

    # 全量按依赖序重放（含未变模块）：reload 不级联依赖，消费者必须自己
    # 重执行才能绑定被依赖者的新对象（prompts 变更经 route 重放生效）
    replay = _replay_order(tracked)
    reloaded: List[str] = []
    failed: List[str] = []
    with _ReplaceMode():
        for name in replay:
            if _reload_module(name):
                reloaded.append(name)
            else:
                failed.append(name)

    # 刷新成功模块的 mtime 基线（失败的不刷——下次 reload 重试）
    for name in reloaded:
        path = _module_path(name)
        if path is not None and path.exists():
            _MODULE_MTIMES[name] = path.stat().st_mtime

    logger.info("[reload] 变更 %s，重放 %d 个模块（失败 %s）",
                changed, len(reloaded), failed or "无")
    return {"changed": changed, "reloaded": reloaded, "failed": failed}


def reload_all() -> Dict[str, List[str]]:
    """全量重载入口（API 端点 / CLI / watcher 共用）。

    config 缓存直接丢弃（settings 的指纹检查是主防线，这里防御时钟
    回拨；下一次解析自然重读）；代码模块走 :func:`reload_changed`。
    """
    from nexus import settings

    settings.invalidate_config_cache()
    result = reload_changed()
    result["config"] = "invalidated"
    return result


def rebind_sessions(sessions: Dict, pattern_registry) -> int:
    """把内存会话重绑到注册表里的最新 pattern 对象。

    reload 后旧 pattern 对象仍能跑完在途轮次，但拓扑/提示词永远是旧的；
    本函数按 pattern_code 重取注册表现值并重建 module_map/node_map 引用。
    会话中断言旧对象身份的测试不受影响（重绑是显式调用，不自动发生）。

    Args:
        sessions: session_id -> Session 字典（原地修改）
        pattern_registry: ``nexus.registry.patterns.registry``

    Returns:
        重绑成功的会话数（pattern 已注销的会话跳过并告警）。
    """
    rebound = 0
    for session in list(sessions.values()):
        new_pattern = pattern_registry.get(session.pattern_code)
        if new_pattern is None:
            logger.warning("[reload] 会话 %s 的 pattern '%s' 已注销，保持旧引用",
                           getattr(session, "session_id", "?"), session.pattern_code)
            continue
        if new_pattern is session.pattern:
            continue  # 未变更（本轮没有重载该 pattern）
        session.pattern = new_pattern
        session.cxt.module_map = new_pattern.module_map
        session.cxt.node_map = new_pattern.node_map
        rebound += 1
    return rebound


# ============================================================================
# 可选后台 watcher（轮询 mtime；默认不开）
# ============================================================================

class ReloadWatcher:
    """后台线程轮询 mtime，变更即自动 reload_all。

    生产默认不启用（推荐显式 API/CLI 触发，可控可观测）；开发期长驻
    进程想免手动触发时开启（环境变量 ``NEXUS_RELOAD_WATCH=1``）。
    uvicorn --reload 整进程重启，管不到进程内注册表——这个 watcher 补
    的就是"不重启进程刷新注册表"的场景。
    """

    def __init__(self, interval: float = 2.0, on_reload=None):
        self._interval = interval
        self._on_reload = on_reload or reload_all
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "ReloadWatcher":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="nexus-reload-watcher", daemon=True)
        self._thread.start()
        logger.info("[reload] watcher 已启动（interval=%.1fs）", self._interval)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2 + 1)
            self._thread = None

    def _run(self) -> None:
        init_baseline()  # 避免启动瞬间的既有文件被误判为"变更"
        while not self._stop.wait(self._interval):
            try:
                result = self._on_reload()
                if result.get("changed"):
                    logger.info("[reload] watcher 自动重载: %s", result["changed"])
            except Exception:  # noqa: BLE001 -- watcher 永不退出
                logger.exception("[reload] watcher 轮次异常（继续）")


_watcher: Optional[ReloadWatcher] = None


def start_watcher(interval: float = 2.0) -> ReloadWatcher:
    """启动全局 watcher（幂等；环境变量 NEXUS_RELOAD_WATCH=1 时 main.py 开机自启）。"""
    global _watcher
    if _watcher is None:
        _watcher = ReloadWatcher(interval=interval).start()
    return _watcher


def stop_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None
