# nexus-kit architecture — what an app builder must know

Source of truth: the code. This doc cites file paths — when in doubt, read
them (and cross-check with `python nexus-introspect-skill/introspect.py ...`).

## The one-line model

> Any business process = **one Pattern** (the data template: a graph of
> BaseNodes) + **a set of plugins** (executors / stages / tools).

An "app" is a directory under `apps/` that declares a Pattern and registers
whatever plugins its nodes need. `host/main.py` auto-discovers both by AST
scanning `apps/*/*.py` for top-level `registry.register(...)` calls — there
is no central registry file to edit.

## Layers (enforced)

| Layer | Dir | Contains | Rule |
|---|---|---|---|
| Kernel | `nexus/` | Pattern/BaseNode model (`nexus/model/`), chat engine (`nexus/engine/`), registries (`nexus/registry/`), settings, LLM providers | imports nothing above it |
| Atoms | `atoms/` | Default executors (`atoms/executors/`), builtin stages (`atoms/stages/`), builtin tools (`atoms/tools/`), hooks, providers, MCP | may import `nexus` |
| Apps | `apps/` | One dir per app: `route.py` + optional `executor.py` / `stages.py` / `prompts.py` / `tools.py` / `config.yaml` | may import `nexus` + `atoms` |
| Host | `host/` | FastAPI assembly, discovery, reload | imports everything |

The contract `host -> apps -> atoms -> nexus` is enforced twice:
`tests/test_architecture.py` (walks every import in every file) and
import-linter in `pyproject.toml`. Your app writing into `apps/` can never
break it — unless you edit other layers, which you must not.

## Graph types — exact semantics

There are exactly two (`PATTERN_TYPES` in `nexus/model/pattern.py:35`);
anything else raises at construction. Dispatch happens in
`nexus/engine/chat.py` (`_run_fsm_turn` / `_run_agent_graph`).

### `pattern_type="fsm"` — one node per user turn

- The FSM pipeline executor (`pattern.plugins["fsm"]` or `default_fsm`,
  `atoms/executors/fsm_executor.py`) runs the **stages skeleton** — e.g.
  `stages=[{"query": "time_aug_query"}, {"nlu": "install_unified"},
  {"clarify": "..."}, {"nlg": "nlg_pass_through"}]` — and the NLU result's
  `next_node` (validated against the current node's `sub_nodes`) moves
  `cxt.current_node_code` **at end of turn**. One user message = one node
  advance. Cycles (re-ask, reschedule) are natural semantics; there is no
  step budget.
- Slots: nodes declare `slots={name: description}`; NLU fills
  `cxt.filled_slots` incrementally per transition.
- FSM rejects fan-out (`sends`) — `chat.py` raises on that path.
- Stage resolution is two-layer: `node.stages[slot]` > pattern skeleton
  entry > builtin default. Builtin codes worth knowing: `time_aug_query`
  (relative-time rewrite), `nlg_pass_through`.

### `pattern_type="agent"` — whole graph per user message

`_run_agent_graph` (`nexus/engine/chat.py:513`):

1. A fresh turn starts at `pattern.entry_node_code`.
2. Loop: resolve the node's executor — `node.plugins["loop"]` >
   `pattern.plugins["loop"]` > `default_loop` (a ReAct-style tool loop) —
   and run it once (`NodeExecutor.execute(ec) -> TurnResult`).
3. Routing: `TurnResult.next` must be one of the node's `sub_nodes`
   (declared adjacency is authoritative; the engine tolerates a hallucinated
   target by terminating with a warning). `next=None` + no successors /
   `is_end` → terminate. `TurnResult.sends` → concurrent fan-out workers,
   results settled onto the `graph_state["__fanout_results__"]` board, then
   the merge node (the one common successor) runs.
4. `wait_human=True` → the graph pauses AT this node; the cursor
   (`__paused_node__`) and step counter persist (sessions table); the next
   user message re-executes the same node with `ec.resume_input` set.
   Forbidden inside a fan-out branch.
5. Termination also fires when `config.max_steps` (default 10) is exhausted —
   the engine force-closes with a truncated reply.

Key behavioral difference for design: fsm nodes "are" conversation beats;
agent nodes "are" pipeline stations whose replies are mostly silent
(`content=""`), with exactly the last station's non-empty `TurnResult.content`
becoming the user-visible reply.

## The state board (`cxt.graph_state`)

`DialogueContext.graph_state` (`nexus/context.py:195-209`) is the turn-scoped
dict nodes use to hand off work:

- Engine-reserved keys (never write these yourself): `__paused_node__`,
  `__step__`, `__fanout_results__`.
- Everything else is free; apps conventionally use one namespaced key holding
  a dict (archify: `archify_state`, deep_research: `deep_research_state`).
- Cleared when the graph terminates; persisted across `wait_human`
  suspensions and process restarts (sessions table `graph_state` column).
- Cross-turn business data (surviving whole conversations) belongs in
  `cxt.metadata` / `cxt.filled_slots` instead.

## Executor & tool plumbing (short version — full contracts in plugin-and-tool-guide.md)

- Plugin registry (`nexus/registry/plugins.py`): kinds `executor`, `stage`,
  `stage_factory`, `messages_builder`, `agent_hooks`. Registration is a
  module-level call `registry.register("executor", "my_code", MyClass)`;
  instances are cached and must be **stateless** (all state on `ec.cxt`).
- Tool registry (`nexus/registry/tools.py`): builtin tools under
  `atoms/tools/` (bash, run_python, file tools, delegate_task, run_workflow,
  task list, cron, skills, MCP). A tool is callable by a node only through
  the deny-by-default intersection `pattern.allow_toolset` (toolset names,
  e.g. `shell`, `filesystem`) ∩ `node.use_tools` (tool names), resolved in
  `nexus/engine/loop.py::_resolve_tools`.

## App configuration

- Code-level declaration (route.py) is the source of truth; an optional
  `apps/<name>/config.yaml` overlays it. Currently only
  `apps/archify_agent/config.yaml` exists — read it as the full vocabulary:
  `pattern:` (binding key, required), `llm:` (pattern default + per-node
  override: code/model/temperature/max_tokens/enable_thinking/timeout),
  `loop:` (max_tool_rounds / max_steps / max_fanout), `compression:`,
  `guardrails:`, `skills:`, and the free `config:` bag your executor reads
  via `get_pattern_custom_config(<pattern code>)` (put `workspace_root`).
  Full spec: `docs/design/app-config.md`.
- Global config lives in `host/config/local_config.yaml` (secrets via env
  var references — never in app yaml).

## Runtime & persistence

- Run: `uvicorn host.main:app --port 8000`. APIs: `POST /api/v1/launch`,
  `POST /api/v1/chat` (+SSE `/chat/stream`), `/api/v1/sessions...`, guarded
  by `NEXUS_API_KEY`. UIs: `/console` (ops) and `/studio` (orchestration).
- Sessions/messages persist to `data/dialogue.db` (sqlite; `graph_state`
  snapshot per turn-settle). `data/` is **gitignored runtime output** —
  your app's file workspaces go under `data/<app>/...`.
- Hot reload: `POST /api/v1/reload` re-runs discovery (dev convenience).

## Where to look next

- `references/pattern-schema.md` — the fields you will actually type.
- `references/example-archify-agent.md` / `references/example-install-booking.md`
  — the two exemplars, dissected.
- `ARCHITECTURE.md` (repo root) — the authoritative deep dive.
