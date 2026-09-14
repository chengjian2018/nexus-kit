"""nexus-studio —— 编排工作台（自动编排 / 流程编排 / 模版测试）。

独立于 ui.api（运营配置台 /console）的第二个控制台面：

- api.py      /api/v1/studio/*（pattern 管理面 + agent 生成 SSE + AI 助手）
- store.py    托管目录（host/config/plugins/*.py + host/config/patterns/*.yml）
              的装载器——启动/reload 后重放，console pattern 同 code 覆盖代码版
- agent.py    LLM 提示词构建 / 围栏输出解析 / 生成插件导入验证
- static/     无构建原生 JS 前端，挂载在 /studio（PRD：docs/design/ 目录）

设计决策（用户已确认）：自动编排生成的插件 Python 代码自动落盘注册；
仅装载固定托管目录，不提供装载任意路径的口子。
"""

from pathlib import Path


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"
