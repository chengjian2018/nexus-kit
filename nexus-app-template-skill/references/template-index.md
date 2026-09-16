# Template library — the entries under `app-templates/`

The template library IS the knowledge base: `app-templates/`, one directory
per entry (dir name = pattern code), each a single self-contained
`TEMPLATE.md` (Pattern declaration + placeholder plugin/tool cards, **no
implementation code**). The authoritative catalog is `app-templates/INDEX.md`
(code / name / business shape / graph type / source); this file adds the
"what to borrow each entry for" guidance. Node counts below are the
blueprint's, taken from each entry's 节点清单 — the distilled template is
the authority here, not the live app's wiring.

| Entry | Type | Nodes | Business shape | Borrow it for |
|---|---|---|---|---|
| `app-templates/install_booking_agent/` | fsm | 16 | Outbound booking call: greeting → address → arrival → time negotiation (schedule guard) → confirm → close; decline/callback/reschedule side channels | The **FSM reference** (zero-borrow origin): slots, `stages` skeleton, guarded unified NLU, answer_examples, two-beat endings |
| `app-templates/repair_booking_agent/` | fsm | 14 | Repair-booking variant: same time-negotiation guards; after confirmation, fault-info collection before hang-up | **Cross-app mechanism reuse**: guard machinery subclass-inherited, only node codes + phrase fragments rebound — the declared way to borrow instead of copying |
| `app-templates/ppt_generator_agent/` | fsm | 6 | External-API delivery dialogue: collect topic → template intent → real list (guard-injected) → guarded generation → deliver link; decline side channel | The **external-API delivery variant** (borrows install at mechanism level): deterministic guards riding the unified stage, failure ledger `failed_tpl_ids` (anti-replay), deterministic dwell on failure, offline route-test paradigm |
| `app-templates/customer_agent/` | agent | 2 | E-commerce CS: ReAct tool loop over retrieved knowledge + `[HANDOFF]` marker → same-turn human handoff | The **messages_builder migration paradigm** + handoff-marker protocol source |
| `app-templates/deep_research/` | agent | 4 | preplan → plan → per-subquestion **fan-out** search → join synthesize | The **fan-out recipe source**: `sends` dispatch discipline, `branch_input` payload, `__fanout_results__` join, orphan escape edges |
| `app-templates/topic_research/` | agent | 6 | preplan → topic plan → per-topic fan-out → zero-LLM merge → draft → streaming polish | Longer fan-out pipeline (borrows deep_research's search branches): stateless executor base reuse, zero-LLM merge station, streaming terminal station |
| `app-templates/archify/` | agent | 9 | Diagram engineering: select → author → validate ⇄ repair loop → deliver → browser evidence → percept → report | The **agent + repair-loop discipline source**: `val_history` / `best_checkpoint` / `solver_tried` experience inheritance, stale-N honest exit, budgets |
| `app-templates/archify_skill/` | agent | 1 | Skill-manual execution: one node loads the skill manual via `use_skills` and delivers in one pass | The **minimal app**: single `default_loop` node + `use_skills` (skill assets give knowledge, not permissions) |
| `app-templates/xianyu_agent/` | agent | 5 | Intent-menu CS: root re-run each turn; local-rule + LLM-fallback routing to four menu nodes | Routing-style agent graph; zero-LLM fixed-decline reply for haggle-round control |

## How to borrow (template level)

Borrowing here is **structural, never code**: graph shape, guard discipline,
loop-inheritance strategy, card paradigms. When Phase 2 declares a borrow,
name the source entry and the borrowed mechanism in the 元信息 line
(借用来源=...), exactly the way ppt_generator_agent and repair_booking_agent
declare their mechanism-level borrows of install_booking_agent. Zero borrow
must state why.

- Same business shape as an existing entry → differentiate explicitly or
  merge; never silently duplicate (Phase 0 inventory exists for this).
- Prefer **mechanism borrow** (guards, fan-out recipe, repair-loop
  inheritance, card paradigms) over shape borrow — mechanisms compose
  across business forms; shapes fork.

## Relationship to `apps/`

Every current entry is 逆向自 `apps/<name>` (see each entry's 来源 line).
The app source stays relevant in two places: **reverse mode** reads the
target app's full source and cross-checks with
`python nexus-introspect-skill/introspect.py pattern <code> --view yaml`;
实现注意事项 claims about runtime behavior are grounded in that source.
Forward mode reads templates only — open app source just to verify a
borrowed mechanism's real behavior when the card description is not enough.

The relay back to code (模板 → 实现) lands as a code app under `apps/`
only. The studio fork-to-edit path (`host/config/patterns/`) is
studio-owned territory for non-developers — templates and their
implementations never go there.
