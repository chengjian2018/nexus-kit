"""general_agent — 单节点通用 AI Agent（技能挂载版）。

一个 default_loop 节点 + 全内置能力面：filesystem 六件套读写工具与
shell（bash/run_python）走三层工具授权；nexus-app-builder /
nexus-app-template-builder 两个技能走 allow_skills/use_skills 双层授权，
手册按需装载（load_skill），流程纪律归 SKILL.md 本体。base_prompt 的
逐工具纪律文风借镜 DeepSeek Harness（dsh）的 system prompt。
"""
