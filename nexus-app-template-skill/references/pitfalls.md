# Pitfalls — the traps that bit before

Each item cites where the reference answer lives. Read before Phase 2
(the node interaction table below is a mandatory plan artifact).

## 1. The node interaction table (design BEFORE executors)

Nodes in agent graphs are amnesiac workspaces: each executor starts fresh,
and only what you put on the state board survives. The failure mode: a
"fix" node that re-reads the artifact but forgets what the previous fix
rounds already tried, re-attempting failed moves or re-introducing reverted
changes. Fill one row per node — **including every loop**:

```markdown
| Node | Reads (graph_state) | Writes (graph_state) | Routing out |
|---|---|---|---|
| draft | — (entry: init) | `my_state{request, workspace, artifact_path}` | → review |
| review | `my_state.artifact_path` | `my_state.verdicts[] += {pass, issues[]}` | pass→deliver; rounds≥N→deliver(honest); else→revise |
| revise | `my_state{artifact_path, verdicts[], fix_log[]}` | `my_state.fix_log[] += summary; artifact bytes` | → review |
| deliver | `my_state{artifact_path, verdicts, fix_log, done_reason}` | `cxt.metadata[...]` (final trace) | terminal (is_end) |

Loop inheritance (revise ← rounds 1..N-1): verdicts history + fix_log of
applied fixes + best-known checkpoint + tried-and-failed keys.
Convergence signal: <objective metric per round>; stale rule: <N rounds
without a new minimum → honest exit, exit edge declared to deliver>.
```

The archify implementation of exactly this table:
`apps/archify_agent/executor.py` — `val_history` (objective metric history),
`best_checkpoint` (rollback source), `repair_log` + `solver_tried`
(anti-replay memory), `design_notes` (author→repair intent bridge),
`_trailing_stale` (the computed, never guessed, stale rule).

## 2. graph_state rules

- Reserved keys you must never write: `__paused_node__`, `__step__`,
  `__fanout_results__` (`nexus/context.py:195`).
- One namespaced key per app (`"<app>_state": {...}`) — never scatter
  top-level keys.
- Cleared at graph termination; survives `wait_human` pauses and restarts.
  Cross-conversation data → `cxt.metadata` instead.
- **Absolute paths only** on the board. Relative paths resolve against the
  service CWD for file tools but against the bash `workdir` for CLI calls —
  the same string becomes two different files (real archify incident,
  `_absolutize` docstring, executor.py:323). Per-session workspaces:
  `data/<app>/<re.sub(r"[^A-Za-z0-9_-]+","_",session_id)>/`.

## 3. Loop budgeting (max_steps)

AGENT graphs die at `config.max_steps` (default 10) with a force-close.
A validate⇄repair cycle costs **2 steps per round**; archify's stale-5 exit
costs ~16 end-to-end, so it declares 20. Formula:
`max_steps ≥ trunk_steps + 2 × worst_loop_rounds + headroom`. Stack it with
a semantic stop (stale-N honest exit) and per-visit executor rounds — three
independent guards, like archify.

## 4. Fan-out workers are blind

`Send(node_code, input)` workers run in a private workspace with **no
session history** — `ec.branch_input` (the `Send.input` payload) is all
they see. Folding the needed context into `input` is the dispatching node's
job (`nexus/engine/turn_result.py` Send docstring). `wait_human` inside a
branch fails (by design). Worker content lands only on
`__fanout_results__` — it never becomes the user reply directly. Reference:
`apps/deep_research_agent/executor_multi.py`.

## 5. FSM: node-level NLG resolves one turn late

FSM transitions fire end-of-turn; a node-level nlg therefore styles the
NEXT turn's reply and clobbers it. Same-turn deterministic rewrites ride
the stage that chose the transition (`install_unified`'s guard-rewrite in
`apps/install_booking_agent/route.py:75-80`).

## 6. Reply semantics in agent graphs

The user-visible reply = the **last non-empty `TurnResult.content`** along
the run. Intermediate stations return `content=""` (silent) and stream
their work chatter via `ec.stream` with `forward_text=False`. Terminal
stations own the reply; force-close (`ec.force_close=True`) must return an
honest "budget exhausted" reply, never a fabricated success.

## 7. Fork collisions

Plugin codes are global. A forked app that keeps the template's executor/
stage codes raises `插件冲突` at import (same code, different factory).
Rename plugin codes + `plugins={"loop": ...}` bindings + state key +
`get_pattern_custom_config("<code>")` + config.yaml `pattern:` key. The
same rules (and why) live in `references/pattern-schema.md` under
"Registration & discovery".

## 8. Declaration guards (fail fast at construction)

- AGENT node with `slots` → raises. Put business state in `config` free
  fields for agent nodes; `slots` is fsm-only.
- AGENT pattern with `stages` → raises (stages pipeline is fsm-only).
- Any `sub_nodes` target not in `nodes` → raises (dangling edge).
- This means the constructor already reviews your graph's shape — but NOT
  its semantics (a declared-but-wrong edge passes; only route tests catch it).

## 9. Tools are deny-by-default twice

`use_tools=[]` on a node means ZERO tools even when the pattern grants the
toolset; `allow_toolset=[]` means no toolsets at all. If a station "can't
see" its tool, check both layers (and `check_fn` availability) before
touching anything else. Never widen grants to fix a symptom — narrow the
node's needs instead.

## 10. Don't trust the model with placement

When a station's contract is "write the artifact to path X", the executor
owns placement: state the absolute path in the prompt, and after the tool
rounds verify the file landed (adopt-or-fail — archify `_ensure_candidate`,
executor.py:865: when the model wrote the right content to a wrong path,
the executor pins it back; content is the model's, placement is code's).
