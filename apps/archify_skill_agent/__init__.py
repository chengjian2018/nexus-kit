"""archify_skill_agent — a "manual-style" single-node runtime for the
archify skill.

Fully independent of apps/archify_agent (the nine-node workflow version
that compiles SKILL.md's acceptance discipline into graph gates): this app
validates the other route — the skill as a data asset (nexus/skills.py
scanning + the allow_skills/use_skills two-layer grant) loaded on demand by
a single default_loop node, with all process discipline left to the
SKILL.md manual itself.
"""
