---
name: nexus-introspect
description: 内省 nexus-kit 的应用 / pattern / 插件（stage、executor、messages_builder 等）/ 模板知识库（app-templates/ 蓝图条目）。当 agent 需要了解 apps/ 下有哪些应用及其注册物、查看某个 pattern 的结构或生效装配（stages/executor 三层解析结果）、取某个插件码的实现源码纯文本、评估改动一个插件会影响哪些 pattern、盘点 app-templates/ 有哪些模板或取某模板的 Pattern 声明（树/YAML）时使用。只读查询，无副作用。
---

# nexus-introspect

在**仓库根目录**用项目 venv 的 python 运行（`.venv/bin/python` 或已激活环境）：

```bash
PY=nexus-introspect-skill/introspect.py
```

## 用例 → 命令

| 想知道 | 命令 |
|---|---|
| 项目有哪些应用，各注册了什么 pattern / 插件 | `python $PY apps` |
| 插件总览（可按 kind / 归属过滤） | `python $PY plugins [--kind stage\|executor\|messages_builder\|stage_factory] [--owner apps\|atoms\|nexus]` |
| 某 pattern 的结构树 | `python $PY pattern install_booking_agent` |
| 某 pattern 的声明 YAML（round-trip 格式） | `python $PY pattern install_booking_agent --view yaml` |
| 某 pattern 每个**模块的生效装配**（与运行时同口径：module>pattern>骨架>内置默认 四层解析 + executor 链） | `python $PY pattern install_booking_agent --view resolved` |
| 某个插件码的实现源码（纯文本，直接可读/可引用） | `python $PY plugin stage install_unified` |
| 反向索引：谁在声明里引用了这个插件 | `python $PY who-uses stage install_unified` |
| 模板知识库（app-templates/）有哪些条目 | `python $PY templates` |
| 某模板的声明树 / 声明 YAML（未注册蓝图） | `python $PY template ppt_generator_agent` / `--view yaml` |

所有子命令支持 `--json`（结构化输出，供程序消费）；`plugin` 支持 `--no-source`（只要元数据）。

## 语义约定（重要）

- **装配口径 = 运行时口径**：`resolved` 视图复用 `nexus/pipeline.resolve_stage_code`
  与 `builtin_generate_default`，executor 链镜像 `chat._resolve_executor_code`——
  查到的就是运行时生效的，不是手工遍历声明的近似。
- **源码提取覆盖三种注册形态**：类/函数本体（`inspect.getsource`）、
  lambda 包装（`lambda: fn` → 闭包/`co_names` unwrap 到真实实现）、工厂函数
  （工厂体 + AST 扫出产物类，双段输出）；`builtin:<factory>#<n>` 标记经
  `pipeline._resolve_builtin_stage` 解析到类。
- **who-uses 只含直接声明引用**：继承复用（如 repair 子类复用 install 守卫）
  不构成声明引用；继承关系看 `plugin` 的源码输出自然可见。
- **归属**按实现文件路径推断（`apps/<name>/` → 该 app；`atoms/`、`nexus/` → 内核）；
  app→插件映射按「谁 import 时注册」归属（每 app 逐个导入 diff 注册表）。
- **模板 = 声明态，非运行时口径**：`templates` / `template` 只解析
  `app-templates/<code>/TEMPLATE.md` 里带 `# nexus-pattern:` 标记的 yaml
  围栏块（锚点约定与 `nexus-app-template-skill` 的 `verify_template.py`
  同源）；构造 Pattern 仅供渲染树视图，**不注册进注册表**——声明里
  尚未实现的插件码以进程内占位工厂满足构造校验。模板专属内容
  （插件步骤卡 / 节点交互表）与结构闸校验归 TEMPLATE.md 本体与
  `verify_template.py`，不在本 skill 职责内。

## 典型工作流

1. 写新 app 前摸底：`apps` 看全貌 → 挑一个相近的 `pattern X --view resolved` 看装配 →
   `plugin stage <码>` 看要参考的实现。
2. 改插件前评估影响面：`who-uses <kind> <code>` → 逐个 `pattern <code> --view resolved` 核对。
3. 排查「声明了但没生效」：对比 `--view tree`（声明原文）与 `--view resolved`（生效装配）的来源层标注。
4. 摸模板知识库：`templates` 看蓝图全貌 → `template <code> --view yaml` 取声明 →
   条目已有实现时对照 `pattern <同名 live code> --view resolved` 看蓝图与实现的差距。

设计文档：`docs/design/introspect-skill.md`（决策与权衡的完整版）。

> 注：本 skill 未挂到 `.zcode/skills/`（该目录现挂 nexus-app-builder 与
> nexus-app-template-skill 两个软链），不会被自动发现；agent 按上表直接调用
> 脚本即可（或自行把本目录 symlink 进 `.zcode/skills/` 恢复自动发现）。
