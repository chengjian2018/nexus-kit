"""archify_skill_agent — archify 技能的「说明书式」单节点运行时。

与 apps/archify_agent（九节点 workflow 版，把 SKILL.md 的验收纪律编译成
图闸门）完全独立：本应用验证的是另一条路线——技能作为数据资产
（nexus/skills.py 扫描 + allow_skills/use_skills 双层授权）被单个
default_loop 节点按需装载，全部流程纪律交给 SKILL.md 手册本身。
"""
