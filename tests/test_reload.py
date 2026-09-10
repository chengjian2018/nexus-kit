"""热重载机制测试：llm config mtime 缓存 + pattern/plugin/channel re-import。

Config 侧：指纹命中不重读文件 / 文件变更自动重解析 / invalidate 与
reload 编程入口 / 返回深拷贝互不污染。
代码侧：host.reload 的 mtime 检测、replace 模式收编、依赖序重放、会话
重绑、channel spec 每请求活取。
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus import settings
from nexus.settings import (
    get_llm_config,
    invalidate_config_cache,
    load_config,
    reload_config,
)


_LLM_MIN = """\
llm_default:
  code: openai
  model: qwen3.8-max
"""


def _write(tmp_path, text, name="local_config.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _bump_mtime(path, delta=10.0):
    """显式推移 mtime（部分文件系统 mtime 粒度粗，同秒重写指纹不变）。"""
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + delta))


# ============================================================================
# llm config: mtime 指纹缓存
# ============================================================================

def test_config_cache_hit_skips_file_read(tmp_path):
    """指纹未变：返回缓存，不重读文件（read 次数不增加）。"""
    path = _write(tmp_path, _LLM_MIN)
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        load_config(path)
        assert spy.call_count == 1
        cfg2 = load_config(path)
        assert spy.call_count == 1  # 缓存命中
    assert cfg2["llm_default"]["model"] == "qwen3.8-max"


def test_config_cache_invalidated_on_edit(tmp_path):
    """文件变更（mtime 推移）：自动重新解析，读到新值。"""
    path = _write(tmp_path, _LLM_MIN)
    assert load_config(path)["llm_default"]["model"] == "qwen3.8-max"

    _write(tmp_path, _LLM_MIN.replace("qwen3.8-max", "qwen-new"))
    _bump_mtime(path)
    assert load_config(path)["llm_default"]["model"] == "qwen-new"


def test_config_deep_copy_no_cache_pollution(tmp_path):
    """调用方改写返回值不污染缓存：下一次 load 仍拿到原始内容。"""
    path = _write(tmp_path, _LLM_MIN)
    cfg = load_config(path)
    cfg["llm_default"]["model"] = "hacked"
    cfg["pattern_llm"]["injected"] = True
    again = load_config(path)
    assert again["llm_default"]["model"] == "qwen3.8-max"
    assert "injected" not in again["pattern_llm"]


def test_reload_config_programmatic_entry(tmp_path):
    """reload_config 强制重读（绕过指纹）；同指纹重写走缓存、强制入口重解析。"""
    path = _write(tmp_path, _LLM_MIN)
    load_config(path)
    # 等长替换 + 精确恢复 mtime 纳秒：伪造"同指纹重写"（粗粒度文件系统
    # 场景）；APFS 纳秒级粒度下正常重写必变指纹（见 invalidation 测试）
    st = os.stat(path)
    _write(tmp_path, _LLM_MIN.replace("qwen3.8-max", "qwen-bbbbbb"))  # 等长
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        assert load_config(path)["llm_default"]["model"] == "qwen3.8-max"
        assert spy.call_count == 0  # 指纹未变，仍走缓存
        reload_config(path)
        assert spy.call_count == 1  # 强制重读
    assert load_config(path)["llm_default"]["model"] == "qwen-bbbbbb"


def test_get_llm_config_uses_cache(tmp_path):
    """三级编排入口 get_llm_config 同样吃到缓存（每轮 R1 的热路径）。"""
    path = _write(tmp_path, _LLM_MIN + "\npattern_llm:\n  p1:\n    model: pm\n")
    assert get_llm_config("p1", config_path=path)["model"] == "pm"
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        get_llm_config("p1", config_path=path)
        assert spy.call_count == 0


# ============================================================================
# registry replace 模式
# ============================================================================

def test_plugin_registry_replace_mode():
    from nexus.registry.plugins import PluginRegistry

    class _A:
        pass

    class _B:
        pass

    reg = PluginRegistry()
    reg.register("executor", "x", _A)
    with pytest.raises(ValueError):
        reg.register("executor", "x", _B)  # 默认严格：不同 factory 拒绝
    reg.replace_on_conflict = True
    reg.register("executor", "x", _B)  # replace 窗口：替换
    reg.replace_on_conflict = False
    inst = reg.resolve("executor", "x")
    assert isinstance(inst, _B)


def test_plugin_registry_replace_drops_cached_instance():
    from nexus.registry.plugins import PluginRegistry

    class _A:
        pass

    class _B:
        pass

    reg = PluginRegistry()
    reg.register("executor", "x", _A)
    assert isinstance(reg.resolve("executor", "x"), _A)
    reg.replace_on_conflict = True
    reg.register("executor", "x", _B)
    reg.replace_on_conflict = False
    assert isinstance(reg.resolve("executor", "x"), _B)  # 实例缓存被清


def test_channel_registry_replace_mode():
    from nexus.registry.channels import ChannelRegistry

    class _SpecA:
        name = "a"
        payload_model = None

        def parse(self, p):  # pragma: no cover
            raise AssertionError

        def build_reply(self, r, s):  # pragma: no cover
            raise AssertionError

    class _SpecB(_SpecA):
        pass

    reg = ChannelRegistry()
    a = _SpecA()
    reg.register(a)
    with pytest.raises(ValueError):
        reg.register(_SpecB())  # 默认拒绝同名
    reg.replace_on_conflict = True
    b = _SpecB()
    reg.register(b)
    reg.replace_on_conflict = False
    assert reg.get("a") is b
    # 同对象重复注册幂等（不开 replace 也不报错）
    reg.register(b)


def test_channel_router_uses_live_spec():
    """router handler 每请求活取 registry 的 spec：replace 后下一请求生效。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from nexus.channels.base import EngineOps, InboundMessage
    from nexus.channels.webhooks import build_channel_router
    from nexus.registry.channels import ChannelRegistry

    class _Payload(BaseModel):
        user_id: str

    class _SpecV1:
        name = "hotchan"
        payload_model = _Payload
        default_pattern_env = "X"
        token_env = None
        stale_seconds = 300.0

        def parse(self, p):
            return InboundMessage(channel=self.name, text="v1",
                                  session_key=p.user_id)

        def build_reply(self, reply, session_id):
            return {"reply": "v1", "session_id": session_id}

    class _SpecV2(_SpecV1):
        def parse(self, p):
            return InboundMessage(channel=self.name, text="v2",
                                  session_key=p.user_id)

        def build_reply(self, reply, session_id):
            return {"reply": "v2", "session_id": session_id}

    reg = ChannelRegistry()
    v1 = _SpecV1()
    with patch("nexus.registry.channels.registry", reg):
        reg.register(v1)
        app = FastAPI()

        async def _launch(*a, **k):  # pragma: no cover
            return None, "500", "unused"

        async def _run(s, q):
            return q, None

        app.include_router(build_channel_router(
            v1, EngineOps(get_session=lambda _s: SimpleNamespace(session_id=_s),
                          launch_session=_launch, run_chat_turn=_run)))
        client = TestClient(app)
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v1"

        # 热替换（不开 replace 开关直接改 dict，模拟注册表内容更新）
        reg._channels["hotchan"] = _SpecV2()
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v2"

        # 注销后回落到装配时 spec（不 500）
        reg._channels.pop("hotchan")
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v1"


# ============================================================================
# host.reload: mtime 检测 + 依赖序重放 + 会话重绑
# ============================================================================

class ReloadHarness:
    """tmp 目录里的自注册 pattern/plugin/channel 模块 + host.reload 驱动。

    写真实 .py 文件、真实 import、真实 mtime 推移——不走任何 mock 捷径，
    验证的就是"改文件 → reload → 注册表拿新对象"这条完整链路。模块用
    ``_reload_test.`` 前缀的独一名挂在 sys.modules（fixture patch 名单
    生成为 tmp 根的等价物）。
    """

    def __init__(self, tmp_path):
        self.root = tmp_path
        (self.root / "apps" / "demo_app").mkdir(parents=True)
        (self.root / "atoms" / "executors").mkdir(parents=True)
        # apps/__init__ / atoms 包不需要——扫描直接 glob 目录，模块经
        # spec_from_file_location 以独一名导入
        self._counter = 0

    def write_module(self, relpath: str, body: str) -> Path:
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def unique(self, prefix="mod") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"


_PATTERN_MODULE = '''\
"""Self-registering pattern module (reload test fixture)."""
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

registry.register(Pattern(
    code="reload_demo",
    name="reload demo v{ver}",
    description="d",
    entry_module_code="m1",
    modules=[AgentModule(module_code="m1", module_name="m1",
                         module_description="d", module_todo_description="t",
                         sub_modules=[])],
))
'''

_PLUGIN_MODULE = '''\
"""Self-registering plugin module (reload test fixture)."""
from nexus.registry.plugins import registry


class _Exec{ver}:
    def execute(self, ec):  # pragma: no cover -- never executed
        raise AssertionError


registry.register("executor", "reload_demo_exec", _Exec{ver})
'''


def _import_out_of_repo(harness, dotted, relpath):
    """以独一模块名导入 repo 外的注册模块（同 channel 测试的桥）。

    sys.modules 里逐级放置祖先包：**直接父包**的 ``__path__`` 指向文件
    真实目录（``importlib.reload`` 靠它重找 spec）；更上层祖先只需占位
    （模块体内跨模块 import 的名字解析逐级查 sys.modules，不走到磁盘）。
    重执行读到新内容靠 mtime 变化使 pyc 缓存失效（_bump_mtime 保证）。
    """
    import importlib.util
    import types

    parts = dotted.split(".")
    path = harness.root / relpath
    for i in range(1, len(parts)):
        ancestor = ".".join(parts[:i])
        if ancestor in sys.modules:
            continue
        pkg = types.ModuleType(ancestor)
        pkg.__path__ = [str(path.parent)] if i == len(parts) - 1 else []
        sys.modules[ancestor] = pkg

    spec = importlib.util.spec_from_file_location(dotted, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def reload_env(tmp_path, monkeypatch):
    """隔离的 host.reload 环境：tmp 扫描根 + 干净的注册表状态。"""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry
    from nexus.registry.plugins import registry as plugin_registry

    harness = ReloadHarness(tmp_path)
    # 生产 discovery 按 sys.modules 名字前缀（apps./atoms.executors.）过滤；
    # repo 外的测试模块用 _reload_test. 前缀——patch 名单生成，把独一名交回
    monkeypatch.setattr(hr, "_discover_module_names", _make_discover(harness))
    # 清掉其它测试可能留下的 mtime 基线（模块名带 _reload_test 前缀互不
    # 冲突，但同名重跑会误判变更）
    hr._MODULE_MTIMES.clear()
    yield harness
    pattern_registry.deregister("reload_demo")
    plugin_registry.deregister("executor", "reload_demo_exec")
    for name in list(sys.modules):
        if name.startswith("_reload_test."):
            del sys.modules[name]


def _make_discover(harness):
    """替代 _discover_module_names：枚举 tmp 根下已加载模块（不限于注册
    模块——镜像生产语义），按 sys.modules 插入序排（依赖先于消费者）。"""
    def _discover():
        order = {n: i for i, n in enumerate(list(sys.modules))}
        names = []
        apps_root = harness.root / "apps"
        for app_dir in sorted(p for p in apps_root.iterdir() if p.is_dir()):
            for path in sorted(app_dir.glob("*.py")):
                if path.name == "__init__.py":
                    continue
                name = f"_reload_test.apps.{app_dir.name}.{path.stem}"
                if name in sys.modules:
                    names.append(name)
        exec_root = harness.root / "atoms" / "executors"
        if exec_root.is_dir():
            for path in sorted(exec_root.glob("*.py")):
                if path.name == "__init__.py":
                    continue
                name = f"_reload_test.executors.{path.stem}"
                if name in sys.modules:
                    names.append(name)
        return sorted(names, key=lambda n: order.get(n, 1 << 30))

    return _discover


def test_reload_detects_and_replaces_pattern(reload_env):
    """pattern 文件变更 → reload → 注册表里是新对象（name 带 v2）。"""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    path = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    assert pattern_registry.get("reload_demo").name == "reload demo v1"
    old = pattern_registry.get("reload_demo")
    hr.reload_changed()  # 首跑只建立 mtime 基线（watcher 启动时同此）

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py",
                            _PATTERN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.route" in result["changed"]
    assert result["failed"] == []
    new = pattern_registry.get("reload_demo")
    assert new is not old
    assert new.name == "reload demo v2"
    # 幂等：无再变更时第二次 reload 是 no-op
    again = hr.reload_changed()
    assert again["changed"] == []


def test_reload_replaces_plugin_class(reload_env):
    """plugin（executor）文件变更 → replace 模式收编新类，实例缓存刷新。"""
    import host.reload as hr
    from nexus.registry.plugins import registry as plugin_registry

    path = reload_env.write_module(
        "atoms/executors/demo_exec.py", _PLUGIN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.executors.demo_exec",
                        "atoms/executors/demo_exec.py")
    v1 = plugin_registry.resolve("executor", "reload_demo_exec")
    assert type(v1).__name__ == "_Exec1"
    hr.reload_changed()  # 建立基线

    time.sleep(0.02)
    reload_env.write_module("atoms/executors/demo_exec.py",
                            _PLUGIN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)

    result = hr.reload_changed()
    assert result["failed"] == []
    v2 = plugin_registry.resolve("executor", "reload_demo_exec")
    assert type(v2).__name__ == "_Exec2"
    assert v2 is not v1


def test_reload_failure_keeps_old_registration(reload_env):
    """重放失败（语法错误）→ 告警并保持旧注册，其余模块不受影响。"""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    p_pattern = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    hr.reload_changed()  # 建立基线

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py", "def broken(:\n")
    _bump_mtime(p_pattern, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.route" in result["changed"]
    assert result["failed"] == ["_reload_test.apps.demo_app.route"]
    assert pattern_registry.get("reload_demo").name == "reload demo v1"


def test_reload_rebind_sessions(reload_env):
    """rebind_sessions：重载后内存会话切到新 pattern 对象并重建 map。"""
    import host.reload as hr
    from nexus.engine.session import Session
    from nexus.registry.patterns import registry as pattern_registry

    path = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")

    session = Session(session_id="s1", pattern_code="reload_demo")
    session.pattern = pattern_registry.get("reload_demo")
    session.cxt.module_map = session.pattern.module_map
    old_pattern = session.pattern
    hr.reload_changed()  # 建立基线

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py",
                            _PATTERN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)
    hr.reload_changed()

    sessions = {"s1": session}
    assert hr.rebind_sessions(sessions, pattern_registry) == 1
    assert session.pattern is not old_pattern
    assert session.pattern.name == "reload demo v2"
    assert session.cxt.module_map is session.pattern.module_map

    # 已注销 pattern 的会话：保持旧引用
    pattern_registry.deregister("reload_demo")
    sessions = {"s1": session}
    assert hr.rebind_sessions(sessions, pattern_registry) == 0
    assert session.pattern is not None


def test_reload_all_invalidates_config(reload_env):
    """reload_all 失效 config 缓存：同指纹重写后的下一次解析重读文件。"""
    import host.reload as hr

    path = _write(reload_env.root, _LLM_MIN)
    load_config(path)
    # 等长替换 + 精确恢复 mtime 纳秒：伪造"同指纹重写"，只有缓存失效能救
    st = os.stat(path)
    _write(reload_env.root, _LLM_MIN.replace("qwen3.8-max", "qwen-cccccc"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    result = hr.reload_all()
    assert result["config"] == "invalidated"
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        assert load_config(path)["llm_default"]["model"] == "qwen-cccccc"
        assert spy.call_count == 1  # 失效后必然重解析
    invalidate_config_cache(path)


# ============================================================================
# 依赖序：非注册模块（prompts）变更经重放消费者生效
# ============================================================================

_PROMPTS_MODULE = '''\
"""纯数据模块（无任何注册）——reload 依赖序的连带刷新验证。"""
TITLE = "v{ver}"
'''

_DEP_PATTERN_MODULE = '''\
"""依赖 prompts 的注册模块：Pattern name 取自 prompts.TITLE。"""
from _reload_test.apps.demo_app.prompts import TITLE
from nexus.model.module import AgentModule
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

registry.register(Pattern(
    code="reload_demo",
    name=TITLE,
    description="d",
    entry_module_code="m1",
    modules=[AgentModule(module_code="m1", module_name="m1",
                         module_description="d", module_todo_description="t",
                         sub_modules=[])],
))
'''


def test_reload_prompts_change_propagates_via_consumer(reload_env):
    """非注册模块（prompts）变更：重放按 sys.modules 插入序先刷 prompts
    再重放 route（消费者），注册表里的 Pattern 拿到新 TITLE——reload 不
    级联依赖，消费者必须自己重执行才能绑定新对象。"""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    reload_env.write_module("apps/demo_app/prompts.py",
                            _PROMPTS_MODULE.format(ver=1))
    reload_env.write_module("apps/demo_app/route.py",
                            _DEP_PATTERN_MODULE.format())
    # 只显式 import route：prompts 由 route 的 import 连带载入（插入序
    # prompts < route，正是生产里 route→prompts 的依赖形态）
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    assert pattern_registry.get("reload_demo").name == "v1"
    hr.reload_changed()  # 建立基线（含非注册模块 prompts）

    time.sleep(0.02)
    p_prompts = reload_env.write_module("apps/demo_app/prompts.py",
                                        _PROMPTS_MODULE.format(ver=2))
    _bump_mtime(p_prompts, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.prompts" in result["changed"]
    assert result["failed"] == []
    # route 未变更但被连带重放 → 注册进注册表的 Pattern 用上新 TITLE
    assert pattern_registry.get("reload_demo").name == "v2"
