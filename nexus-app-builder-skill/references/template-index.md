# Template library — the 8 apps under `apps/`

The template library IS the apps directory: 8 working applications, each a
forkable skeleton. Verified codes/types (re-verify with
`python nexus-introspect-skill/introspect.py apps`):

| App dir | Pattern code | Type | Nodes | Business shape | Borrow it for |
|---|---|---|---|---|---|
| `apps/install_booking_agent/` | `install_booking_agent` | fsm | 16 | Outbound call: greeting → address → arrival → time negotiation (schedule guard) → confirm → close; decline/callback/reschedule side channels | The **FSM reference**: slots, `stages` skeleton, guarded unified NLU, answer_examples, two-beat endings, per-app tests (`tests/test_install_booking_agent_route.py`) |
| `apps/repair_booking_agent/` | `repair_booking_agent` | fsm | — | Repair-booking variant of the above | How to **subclass-reuse** another app's stages (`stages.py:30` imports install's guard machinery) instead of forking |
| `apps/archify_agent/` | `archify` | agent | 9 | Diagram engineering: route → author → validate ⇄ repair loop → deliver → visual-check → percept → report | The **agent + repair-loop reference**: custom executors, state board (`archify_state`), experience inheritance (`val_history`/`best_checkpoint`/`solver_tried`), honest exits, budgets, the only full `config.yaml` |
| `apps/deep_research_agent/` | `deep_research` | agent | 4 (`route_multi.py`) | preplan → plan → search **fan-out** → synthesize | The **fan-out reference**: `TurnResult.sends`, `__fanout_results__` join, `Send.input` payload discipline, orphan escape edges |
| `apps/topic_research_agent/` | `topic_research` | agent | 5 | Five-stage topic research pipeline | Linear multi-stage agent pipeline with per-stage executors |
| `apps/xianyu_agent/` | `xianyu_agent` | agent | 5 | Intent-menu customer service: root routing node → menu nodes | Routing-style agent graph (the ROUTE-era shape expressed as agent); `messages_builder` plugin (`customer_agent_messages_builder` lives in customer_agent) |
| `apps/customer_agent/` | `customer_agent` | agent | 2 | E-commerce CS with RAG + human handoff node | RAG/knowledge grounding + a `human_handoff` (wait_human-style) node |
| `apps/archify_skill_agent/` | `archify_skill` | agent | 1 | Single-node skill-driven diagram app | The **minimal app**: one default_loop node + `use_skills` (skill assets give knowledge, not permissions) |

## How to borrow

**Default: code-level fork.** Copy `apps/<template>/` → `apps/<your_app>/`,
then run the rename checklist below. You inherit the registration idiom,
prompts organization, tests structure, and config wiring for free.

**Structure reference (no copy).** When your graph differs too much from
every template, still read the closest 1–2 before designing, and declare
"zero borrow + why" in the Gate-2 plan.

**NEVER: the studio fork-to-edit path.** `POST /api/v1/studio/patterns/fork`
exports YAML into `host/config/patterns/<code>.yml` — that is studio-owned
territory for non-developers. Code apps live in `apps/` only. (Same for
`host/config/plugins/`.)

## Fork rename checklist

The plugin registry is global: same `(kind, code)` + different factory
raises `插件冲突` at import. After copying a template dir:

1. `route.py`: rename `Pattern(code=...)`, every node `code=` (pattern-local,
   but stale names confuse readers), every `plugins={"loop": "<code>"}` and
   the executor registration codes in `executor.py`/`stages.py` — these are
   GLOBAL and **must** be renamed.
2. Prompt-module names and the bottom `import apps.<name>.executor` path.
3. `config.yaml` (if kept): the `pattern:` binding key.
4. The state-board key (`cxt.graph_state["<template>_state"]`) and any
   `get_pattern_custom_config("<code>")` reads — rename to your pattern code.
5. Workspace root in the config bag: `workspace_root: data/<your_app>`.
6. Tests: copy the template's route test, retarget code/node/plugin names.
7. Grep the copied dir for the template's codes before declaring done:
   `grep -rn "<template_code>" apps/<your_app>/` must return nothing stale.
