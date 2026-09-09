# 计划③ 改动记录：pattern yml round-trip + 校验体系

日期：2026-09-09。测试 493 → 516 全绿（+8 yaml round-trip、+15 校验）。

## 改动清单

### 新增
- `nexus/model/serialization.py`：pattern_to_dict/from_dict/to_yaml/
  from_yaml；模块 type 值映射子类（agent/fsm/route）；node 的 jump_module
  按可选属性序列化；from_dict 走完整构造路径（normalize + 图校验照跑）
- `nexus/model/validation.py`：validate_base_info / validate_plugin_
  declarations / validate_pattern（收集式编号报错）；软警告（name 缺失）
  仅日志；槽位归属骨架校验；stage code 唯一性（nlu/nlg unified 豁免）
- `tests/test_pattern_yaml.py`（8）：round-trip 稳定性 / 结构等价 /
  from_dict 图校验照跑 / 非 mapping yml 报错 / 空 stages 归一默认骨架
- `tests/test_validation.py`（15）：各项校验 / 收集式报错（4 项一次 raise）/
  生产 pattern 全量通过
- host/main.py `_validate_registered_patterns`（startup 末尾，失败
  SystemExit）
- host/cli.py：`pattern-export` / `pattern-load [--validate-only]` 子命令

### 关键实现点
- 校验用 `plugin_registry.has()`（不实例化）——全量 pattern 校验不会
  构造几十个 stage 实例
- 重复 code 判定按 slot 分组：某 code 的槽位集合 ⊄ {nlu,nlg} 才报错
  （query+nlu 共用 code 是错的；nlu/nlg 共用是对的 unified）
- from_dict 弹性：modules 的 nodes 键映射 module_nodes；type 缺省 agent

## 避坑记录（后续计划必读）

1. **yml 的 None 槽位**写法是 `- nlu: null`（safe_dump 显式 null；
   手写 yml 用 `- nlu:` 空值也解析为 None，两种都行）。
2. **dict 顺序即骨架顺序**：yaml safe_load 在 Python 3.7+ 保持插入序，
   round-trip 稳定依赖这一点；不要在 from_dict 里 sort keys。
3. **from_yaml 之后必须 validate**：构造期只跑图校验（悬边/自环），
   plugin code 可解析性靠 validate_pattern（CLI 与 host 都已接）。
   计划⑥给 pattern 加 enable_project 字段时，序列化字段表
   （serialization._MODULE_FIELDS）与校验都要同步加。
4. **校验报错文案带 kind 提示**（kind=executor/stage/...），排查时直接
   知道该 import 哪类原子模块。
5. **host 校验在 startup 而非 import 期**：import 期插件注册表可能未
   预热（discover_builtin_plugins 在模块级调用，但 stage codes 在
   atoms.stages，host/main.py import 链未必触达）。若未来把校验提前到
   import 期，需先确认 atoms.stages 已被导入。
6. **test_validation 的 app 预热**：校验生产 pattern 需要
   `import apps.xianyu_agent.route`（app 自有 stage codes）+ discover_
   builtin_patterns。CI 环境跑单个测试文件时 conftest 只预热 atoms，
   app codes 要测试自己 import。
