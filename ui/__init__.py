"""ui — 运营配置台（ops-console，PRD: docs/design/ops-console-prd.md）。

自包含的组装包：后端 router（ui.api，挂在 /api/v1/console/*，由
host.main include）+ 无构建前端静态资产（ui/static/，由 host.main 挂在
/console）。分层契约（host → apps → atoms → nexus）不覆盖 ui/——它的
依赖方向与 host 层一致（只 import atoms/nexus），由 host 统一装配。

P0 范围：pattern 只读视图（列表 / 详情：yml + mermaid + 声明树）、
插件与工具目录、知识库 CRUD / 空间 / 试搜台。编辑与发布流（P1）不在
本包内。
"""
