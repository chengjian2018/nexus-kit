---
name: nexus-app-builder
description: Convert a business requirement into a nexus-kit application (a Pattern graph + capability plugins under apps/). Use when the user wants to turn a business process, workflow, or requirement into an app on this framework, create a new app under apps/, or design a pattern graph (fsm or agent) with nodes, executors, stages, or tools.
---

# nexus-app-builder

Turn a business requirement into a nexus-kit application: one **Pattern** (the
data template — a graph of nodes) plus **capability plugins** (executors /
stages / tools), living under `apps/<name>/`.

Work in the phases below, in order, fully autonomously — no step pauses for
user confirmation. The Phase 1 decision statement and the Phase 2 plan are
still mandatory artifacts, not permission slips: a wrong graph type or a
wrong node-interaction design means redrawing the whole app.

## The framework in 60 seconds

| Layer | Dir | Role |
|---|---|---|
| Kernel | `nexus/` | Pattern/BaseNode model, chat engine, registries — never modified by apps |
| Atoms | `atoms/` | Default executors (`default_loop`/`default_fsm`), builtin stages, builtin tools |
| Apps | `apps/` | **You write here.** One dir per app: `route.py` (pattern + registration), optional `executor.py`/`stages.py`/`prompts.py`/`tools.py`/`config.yaml` |
| Host | `host/` | FastAPI assembly + auto-discovery (`host/main.py`) |

- An app registers itself at import time via module-level `registry.register(...)`;
  `host/main.py` AST-scans `apps/*/*.py` to discover it. No central file to edit.
- Two graph types only — `pattern_type="fsm"` (one node advance per user turn)
  or `"agent"` (the whole graph runs from entry per user message). Details:
  `references/architecture.md`.
- Nodes share state through `cxt.graph_state` (the state board); a node runs
  via a `NodeExecutor` resolved as `node.plugins > pattern.plugins > type default`.
- Tools are deny-by-default: a tool is callable only if it is in
  `pattern.allow_toolset` (toolset) **and** `node.use_tools` (per node).

## Phase 0 — Recon (facts before opinions)

1. Read the references you need here (index at the bottom of this file).
2. Survey the template library — the 8 apps under `apps/` ARE the templates
   (`references/template-index.md` maps business shapes to apps).
3. Read the source of the 1–2 closest templates. Do not trust this skill's
   copies over the source: **source code is the only truth**, these docs are
   curated and can drift. Cross-check with the introspect CLI:

   ```bash
   PY=nexus-introspect-skill/introspect.py
   python $PY apps                              # what exists, what each registers
   python $PY pattern archify --view yaml       # any pattern's declaration shape
   python $PY pattern install_booking_agent --view resolved
   python $PY plugin executor af_repair         # a plugin's implementation source
   python $PY who-uses stage install_unified    # reverse index: who references it
   ```

## Phase 1 — Graph type decision

Decide `fsm` vs `agent` using these criteria (verified against
`nexus/engine/chat.py`):

- **fsm** — the requirement is a guided conversation: the assistant asks, the
  user answers, and **each user message advances exactly one node**. Signals:
  slot/form collection, multi-turn negotiation, per-turn confirmations,
  natural cycles (re-asking, rescheduling). Reply comes from stages
  (NLU/NLG); executors rarely needed.
- **agent** — **one user message should trigger an end-to-end deliverable**
  (report, artifact, researched answer) by running a multi-node pipeline
  autonomously until termination. Conditional edges are executor routing
  (`TurnResult.next`). Mid-flow human input is possible via `wait_human`
  (graph pauses; the next user message resumes the same node).
- Ambiguous → default **agent** (the framework default,
  `nexus/model/pattern.py:36`), but you must say why the fsm signals don't win.
- A vague requirement does not stall the build: apply the safest default,
  record every assumption you filled, and list the questions to confirm
  in the final report — infer-and-propose, never interrogate.

**State the decision before moving on (no waiting):** the chosen
`pattern_type`, the criteria cited (which signals applied), and a
one-paragraph node sketch (5–10 node codes with one-line purposes) — the
statement feeds the Phase 2 plan. Do not design the full graph yet.

## Phase 2 — Plan

Produce a plan containing **all five** artifacts. A missing node-interaction
table or a silent "zero borrow" makes the plan invalid.

1. **Template borrow list + diff analysis.** Default to a code-level fork:
   copy `apps/<template>/` as your starting skeleton (it carries the route
   registration idiom, prompts organization, config wiring for free). For
   each template state: kept / deleted / modified / added nodes, and why.
   If nothing is worth forking, declare "zero borrow" **explicitly** with
   reasons — skipping the scan silently is not allowed.
   ⚠️ Fork code only. **Never** use the studio fork-to-edit path
   (`POST /api/v1/studio/patterns/fork` writes `host/config/patterns/` —
   studio-owned territory, outside the apps/ boundary you must stay in).
2. **Full node list.** `code / name / purpose / sub_nodes / is_end` for every
   node, plus (fsm) slots and stages skeleton, or (agent) which executor
   (`plugins={"loop": ...}`) each node binds. Smallest graph that satisfies
   the requirement — every node beyond the trunk cites the signal that
   demands it (signal→topology table: `references/pitfalls.md` §11).
3. **Node interaction table (mandatory).** One row per node: which
   `graph_state` keys it **reads** and **writes**, and — for every loop
   (design→verify→fix→verify…) — the **checkpoint & experience-inheritance
   strategy**: how round N inherits what rounds 1..N-1 learned (history
   arrays, best-known checkpoint, tried-and-failed log). Template and worked
   examples: `references/pitfalls.md`. This is the single most
   failure-prone design decision; the framework's reference answer lives in
   `apps/archify_agent/executor.py` (`val_history` / `best_checkpoint` /
   `repair_log` / `solver_tried`).
4. **Capability triage table.** One row per capability in the requirement:

   | Capability kind | Route to |
   |---|---|
   | Open semantic judgment, drafting, style, routing, summarization | **Prompt-native node** (base prompt; default_loop or one LLM call) |
   | Precise numeric calculation, geometry, any deterministic transform, external/domain API | **Tool** (`apps/<name>/tools.py`, app-scoped registration) or deterministic code inside a custom executor — NEVER prompt-only |
   | Orchestration, state bridging, convergence gates, JSON protocols, validation adjudication | **Custom `NodeExecutor`** (`apps/<name>/executor.py`) |

   The rule: if a wrong answer is silently possible (arithmetic, coordinate
   math, spec compliance, "is this file valid"), it does not belong in a
   prompt. `references/plugin-and-tool-guide.md` has the full contracts.
5. **Artifact list.** Every file you will create: `apps/<name>/...` (new dir),
   `tests/test_<app>_route.py` (new), nothing else. Name the pattern code and
   confirm it doesn't collide with existing ones (`python $PY apps`).

**When the plan is written down, proceed straight to Phase 3 — no approval
stop.** The full plan belongs in your final report so the user can audit
the decisions afterwards.

## Phase 3 — Implement

- Fork/copy the template dir, then rename **everywhere the code appears**:
  `route.py` (`Pattern(code=...)`, node codes, plugin codes), `config.yaml`
  (`pattern:` key) if present, and tests. A leftover template plugin code
  raises `插件冲突` at import (registry rejects same code, different factory).
- Files: `route.py` (pattern declaration + `registry.register(pattern)` +
  bottom `import apps.<name>.executor` if you have one), `executor.py` /
  `stages.py` / `prompts.py` as planned, `__init__.py`, `README.zh.md`
  (convention of existing apps). Optional `config.yaml` — only
  `apps/archify_agent/config.yaml` has one; add it when you need per-app
  `llm:` overrides, `loop:` budgets, `guardrails:`, or a `config:` free bag
  (put `workspace_root: data/<app>` there).
- Runtime outputs → `data/<app>/...` only, organized per session
  (`data/<app>/<sanitized-session-id>/` — see the archify idiom). Absolute
  paths in the state board (relative paths resolve differently under file
  tools vs bash workdirs — a real incident, see pitfalls).
- New tools register in `apps/<name>/tools.py` via the same module-level
  idiom. Do NOT put tools into `atoms/tools/` — if a tool is genuinely
  reusable across apps, propose the promotion in your final report instead
  of doing it.

## Phase 4 — Verify (all mandatory; red = not done)

1. `pytest tests/test_architecture.py` — layering gate must stay green.
2. New `tests/test_<app>_route.py`, offline, no network: structure
   assertions (nodes/edges/type), `validate_pattern(pattern)`, and a
   scripted fake-provider dialogue walk. Copy the idiom from
   `tests/test_install_booking_agent_route.py` (fixtures `register_fake_provider`,
   `fake_llm_config`, `discover_builtin_patterns`; helpers in `tests/async_utils.py`).
   This test **is** the smoke test — a green fake-provider walk proves
   registration, dispatch, and your executor wiring end to end.
3. Report honestly: what passed, what is stubbed, what remains. Note what
   a live pilot must still watch — rounds-to-converge, repair success
   rate, tool error rate: a green fake-provider walk proves wiring, not
   semantic quality.

Never declare success with a red or skipped verification step.

## Safety red lines (HARD — no exceptions, no user override needed)

1. **Write only**: `apps/<name>/` (new directory), `data/<name>/` (runtime,
   gitignored), `tests/test_<app>_*.py` (new files only).
2. **Never modify**: `nexus/`, `atoms/`, `host/`, `ui/`, any existing app,
   existing tests, `docs/`, `pyproject.toml`, `skills/`.
3. **Never write** `host/config/patterns/` or `host/config/plugins/`
   (studio-owned), or anything outside the repo root.
4. Secrets never live in app code or config.yaml — env vars /
   `host/config/local_config.yaml` only.
5. Tool authorization stays deny-by-default (`allow_toolset` ∩ `use_tools`);
  never widen grants "to make it work".

## References

| File | What it covers |
|---|---|
| `references/architecture.md` | Layers, both graph types' exact runtime semantics, dispatch, registries, config, persistence |
| `references/pattern-schema.md` | Pattern / BaseNode field tables, YAML round-trip, compile-time validation rules |
| `references/plugin-and-tool-guide.md` | NodeExecutor contract, TurnResult/Send, registration idioms, tool registry, LLM-call idiom, triage rules |
| `references/template-index.md` | The 8 template apps → business-shape mapping; fork checklist |
| `references/example-archify-agent.md` | Dissection of the agent exemplar: 9-station graph, repair loop, state board, budgets |
| `references/example-install-booking.md` | Dissection of the fsm exemplar: stages pipeline, slots, guards, route-test idiom |
| `references/pitfalls.md` | Node-interaction table template, graph_state rules, experience-inheritance patterns, known traps |
| `references/toy-app/` | A complete minimal agent app (draft→review→revise→deliver) to copy as your starting skeleton |
