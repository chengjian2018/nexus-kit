# Exemplar dissection: `apps/archify_agent/` (agent graph + repair loop)

The reference answer for every "produce → verify → fix → verify…" business
loop. Read this together with the source:
`apps/archify_agent/route.py` (declaration), `executor.py` (nine stations),
`prompts.py`, `config.yaml`, tests in `tests/test_archify_agent.py`.

## The graph

```
af_route ──> af_author ──┬───────────────> af_validate ──┬─> af_deliver
 type routing   draft     │                ↑  fail        │       │ success
                │         │   repair loop  │              │       │ failure (escape edge)
                v         │                └── af_repair ─┘       v
         af_update_probe  │                  │    │         af_visual_check
          (one-shot side  ┘                  │    │ stale-5        │
           branch, then → validate)          │    └──honest──> af_percept ──> af_report (is_end)
                                            └──────exit─────────^ image review   assembled from receipts
```

Declaration facts worth copying verbatim:

- Every node binds its station one-to-one: `plugins={"loop": "<node code>"}`
  (plugin code = node code — trivial to keep consistent).
- Escape/failure edges are **declared** (`af_deliver.sub_nodes` includes
  `af_report`): an undeclared routing target terminates the graph.
- `config={"max_steps": 20}` — budgeted for the loop's worst path
  (stale-5 exit costs ~16 steps).
- Tool grants: pattern grants `allow_toolset=["shell", "filesystem"]`; each
  station narrows via `use_tools` — pure-semantic stations
  (`af_route`/`af_percept`/`af_report`) grant **zero** tools.

## Station split — the triage lesson

| Station | LLM? | Tools | Discipline |
|---|---|---|---|
| `af_route` | 1 call, JSON protocol + 1 self-correct retry | none | degrades honestly (defaults to workflow, marks `degraded`) |
| `af_author` | bounded tool workspace (≤ `author_rounds`) | read/write/find | artifact-first: the next action must be writing the candidate file |
| `af_update_probe` | none (deterministic) | bash | one-shot latch (`probe_done`); information is not permission |
| `af_validate` | none | bash | objective gate: CLI receipt decides pass/fail — **never model-judged** |
| `af_repair` | convergence gate first (deterministic), zero-LLM solver, then ≤ `repair_rounds` LLM rounds | read/edit/write/bash | only strict improvements kept; byte-rollback otherwise |
| `af_deliver` | none | bash | non-zero exit is never success |
| `af_visual_check` | none | bash | collects evidence; never modifies the artifact |
| `af_percept` | 1 multimodal call | none | judges only attached screenshots; honest `skipped` when incapable |
| `af_report` | none | none | report ASSEMBLED from receipts, never model prose |

This is the capability-boundary rule made flesh: semantics → model,
geometry/precision → tools, acceptance → receipts.

## The state board (`cxt.graph_state["archify_state"]`)

Initialized once by `af_route` (`_new_state`, executor.py:263), mutated by
every station, saved with `_save_state`. Fields = the inter-node contract:

| Field | Written by | Purpose |
|---|---|---|
| `request`, `diagram_type`, `is_mermaid`, `output_name` | route | what the user asked / routing verdict |
| `workspace`, `candidate_path`, `output_html` | route | **absolute** per-session paths (`data/archify/<sanitized-session>/`) |
| `probe_done`, `update_notice` | probe | one-shot latch + notice text |
| `val_history` | validate | objective error count per validate visit — `[5,3,4,2]` — the convergence signal |
| `last_receipt` | validate/repair | diagnostics summary feeding repair (code/subject/supported_fixes/evidence) |
| `design_notes` | author | the author's closing memo — **the bridge across the author→repair amnesia** (the graph splits them into separate workspaces; without this the repair station forgets the layout intent) |
| `repair_log` | repair | per-visit action summaries — prevents replaying failed moves |
| `solver_tried` | solver | failed geometry-move keys across visits — same anti-replay idea, deterministic |
| `best_checkpoint` | validate | candidate bytes + receipt at each new error minimum — the rollback source |
| `frozen` | validate | pass latch — the candidate is never touched again |
| `deliver_failed`, `*_receipt`, `honest_exit`, `degraded`, `phases` | downstream | receipts + honest-exit markers for the final report |

Every one of these exists because two stations needed to share it. That is
the design discipline: **the state board fields ARE your node interaction
table** — design them before writing executors.

## The repair loop's four guards (steal this stack)

In `AfRepairExecutor.execute` order (executor.py:1298+):

1. **Regression guard** (deterministic, first): if the last validation is
   worse than the historical best, roll the candidate bytes back to
   `best_checkpoint` BEFORE repairing (a live run saw `val_history`
   `[1,1,1,13]` — entering the next round wounded compounds blind patching).
2. **Convergence gate** (deterministic, never model-decided):
   `_trailing_stale(val_history) >= stale_limit` (default 5; app yaml sets 3)
   → honest exit to the report station with unresolved diagnostics. Stop
   polishing; report honestly.
3. **Zero-LLM solver**: for problems that are pure geometry (label
   clearance), compute candidate moves from the diagnostic evidence, keep
   only strictly-better ones (adjudicated by the real validator), byte-rollback
   the rest, remember failed moves in `solver_tried`.
4. **Bounded LLM micro-loop**: ≤ `repair_rounds` (default 3) rounds per
   visit; the graph itself is the macro loop (`af_repair → af_validate →
   af_repair …` via `TurnResult.next`).

Budget stack (three independent dimensions): graph `max_steps=20` ×
stale-N honest exit × per-visit rounds. When you budget your own loops:
`max_steps ≥ trunk_steps + 2 × (worst-case loop rounds) + headroom`.

## config.yaml anatomy (`apps/archify_agent/config.yaml`)

`pattern: archify` binding; `llm:` pattern default + per-node overrides
(including a different provider/model for the vision station `af_percept`);
`loop:` budgets; `guardrails.shell_tool.timeout_seconds` (loosened for the
CLI calls); free `config:` bag read via `get_pattern_custom_config("archify")`
— `workspace_root: data/archify`, `author_rounds`, `repair_rounds`,
`stale_limit`… Executor code carries defaults; the bag overrides. Use this
exact pattern for any tunable you don't want to hardcode.

## Honest-exit philosophy

Every degradation path leaves a receipt instead of pretending: parse
failure → `degraded` flag; no evidence → `skipped` verdict; stale loop →
report with unresolved diagnostics; force-close (step budget) → fixed
"truncated" reply that never claims success. Copy this stance — it is the
difference between an app that fails loudly and one that hallucinates.
