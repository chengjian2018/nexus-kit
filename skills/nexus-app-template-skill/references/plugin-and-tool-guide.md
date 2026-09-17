# Capability plugins: executors, stages, tools — contracts and triage

Curated from `nexus/engine/execution.py`, `nexus/engine/turn_result.py`,
`nexus/registry/plugins.py`, `nexus/registry/tools.py`. Verify against the
files; this copy can drift.

## The three routes for a capability (triage rules)

| Requirement signal | Route | Why |
|---|---|---|
| Open semantic judgment: drafting, style, tone, intent routing, summarize, translate | **Prompt-native node** — `base_prompt` on a `default_loop` node, or one LLM call inside an executor | What LLMs are for |
| Deterministic transform the LLM will fumble: precise arithmetic, geometry/coordinates, date math, spec compliance, "is this file valid", external/domain API call | **Tool** (registered function the model calls), or **deterministic code inside a custom executor** (no LLM in the loop at all) | A wrong answer must not be silently possible. If verification can be done in code, code adjudicates; the LLM only handles what needs semantic judgment |
| Orchestration: multi-step workspace with rounds, JSON protocols, convergence gates, cross-node state bridging, fan-out dispatch | **Custom `NodeExecutor`** | Needs control of `TurnResult` routing + the state board |

Rule of thumb: **semantics belong to the model, geometry/precision/acceptance
belong to tools and deterministic code** (the archify discipline). A
"validate" step that asks the LLM "does this look correct?" is a design bug;
a validator tool/executor that returns an objective receipt is the pattern.
When a check genuinely cannot be coded (taste, coherence, tone), the
LLM-judge node is the fallback, not the default — frame it as refutation
("try to prove this wrong; list concrete violations; an empty list only if
you tried and failed"), never "does this look OK?", and its verdict still
lands on the board as a structured receipt.

## Executor contract (`kind="executor"`)

```python
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.turn_result import TurnResult

class MyStationExecutor(NodeExecutor):
    async def execute(self, ec: "ExecutionContext") -> TurnResult: ...
```

- **Stateless by contract**: one cached instance is shared across sessions —
  ALL state lives on `ec.cxt` (put workflow data in `cxt.graph_state`).
- Resolution: `node.plugins["loop"]` > `pattern.plugins["loop"]` >
  `default_loop` (agent) / `default_fsm` (fsm).
- Registration (module level, bottom of `apps/<name>/executor.py`;
  `route.py` imports the module last):

```python
from nexus.registry.plugins import registry as plugin_registry
plugin_registry.register("executor", "my_station", MyStationExecutor)
```

Same-name/different-factory registration raises `ValueError` — rename codes
when forking.

### ExecutionContext (transient per-execution wrapper)

| Field | Meaning |
|---|---|
| `cxt` | The dialogue context — the only state carrier (`history`, `graph_state`, `filled_slots`, `session_id`, `llm_config`) |
| `pattern` / `node` | The live object graph being executed |
| `force_close` | True when the step budget is exhausted — return an honest closing reply, do not route |
| `stream` | SSE emitter (`emit_trace` for tool events; `_emit_round`/`_stream_round` helpers for rounds) |
| `resume_input` | The user message that resumed a `wait_human` suspension (None otherwise) |
| `step` | 0-based graph step index |
| `branch_id` / `branch_input` | Non-None only inside a fan-out worker instance |

### TurnResult

| Field | Meaning |
|---|---|
| `content` | This execution's reply text. The graph's user-visible reply = the LAST non-empty content along the run; silent intermediate stations return `content=""` |
| `next` | Routing: one node code, MUST be in the executing node's `sub_nodes`. None = terminal if no successors / `is_end` |
| `sends` | Fan-out: `[Send(node_code, input), ...]`, mutually exclusive with `next`. Merge node = the common successor of all targeted workers |
| `wait_human` | Suspend the graph AT this node; next user message re-executes it with `ec.resume_input`. Idempotency across re-execution is YOUR job. Forbidden in fan-out branches |
| `actions` / `extra` | Event channel / structured output bag (trace, usage, payloads) |

## The LLM-call idiom inside a custom executor

Mirror `apps/archify_agent/executor.py` (proven in live runs):

```python
from atoms.executors.loop_executor import _stream_round      # streaming + round events
from nexus.engine.loop import _execute_tool, _resolve_tools  # raw tool dispatch / grants
from nexus.engine.messages import build_agent_messages       # node + history framing
from nexus.llm.resolve import build_provider

provider = build_provider(cxt.llm_config or {})
messages = build_agent_messages(node, cxt, pattern=pattern)
messages.append({"role": "user", "content": PHASE_PROMPT})
result = await _stream_round(provider, messages,
                             llm_config.get("model", "default"),
                             llm_config.get("temperature", 0.7),
                             llm_config.get("max_tokens", 2048), ec.stream)
content = result.get("content", "") or ""      # plus result.get("tool_calls") if tools
```

- Per-node / per-pattern `llm:` overrides from `apps/<name>/config.yaml`
  arrive already merged in `cxt.llm_config` — read, don't re-resolve.
- Tier models per station: cheap/fast for extract-classify-guard, strong
  for synthesize/repair — that is what the per-node `llm:` overrides exist
  for; don't run one model class across a heterogeneous graph.
- JSON protocols: extract the first balanced `{...}` and self-correct with
  ONE retry (see archify `_extract_json_object` + `route_retries`).
- Silent work rounds: stream thinking but withhold the body
  (`forward_text=False`) — station chatter is not the user reply.

## Stage plugins (`kind="stage"`) — FSM pipelines

Stages implement the FSM NLU/NLG/etc. pipeline steps referenced by the
`stages` skeleton. You need them only when a **guided-conversation** app
must customize understanding/generation per turn (e.g. install_booking's
guarded unified NLU). Exemplar: `apps/install_booking_agent/stages.py`
(`install_unified` — one LLM call writing reply + next_node + slots, with a
deterministic bookable-time guard). Register the same way:
`plugin_registry.register("stage", "my_unified", MyUnifiedNLU)`.

Timing trap (bitten before): FSM node transitions fire **end of turn**, so a
node-level NLG only resolves on the NEXT turn and clobbs that turn's reply.
Same-turn deterministic rewrites must ride the stage that chose the
transition (see the `install_unified` plugin card in
`app-templates/install_booking_agent/TEMPLATE.md`).

## Tools (`nexus/registry/tools.py`)

New tools live in **`apps/<name>/tools.py`** (app-scoped; never write into
`atoms/tools/` — propose promotion to maintainers if truly reusable):

```python
from nexus.registry.tools import registry, tool_result, tool_error

def _handle(args: dict) -> str:
    ...compute precisely...
    return tool_result(success=True, value=v)   # or tool_error("bad input")

registry.register(
    name="compute_layout_distance",     # global name — prefix it (myapp_*) to avoid collisions
    toolset="myapp",                    # your own toolset tag
    schema={"description": "...", "parameters": {...json schema...}},
    handler=_handle,                    # or is_async=True with an async handler
)
```

- Handlers take one `args` dict; return a JSON string (use the helpers).
  Sync handlers run in a worker thread — blocking is fine.
- Grants: `pattern.allow_toolset=["myapp"]` + `node.use_tools=["compute_layout_distance"]`.
  Both layers, deny-by-default.
- Builtin toolsets you can request instead of writing new: `shell` (bash,
  run_python), `filesystem` (read_text/write_text/edit_file/list_dir/
  search_files/find_files), `subagent` (delegate_task), `workflow`
  (run_workflow), `tasks`, `cron`, skill tools, `mcp-<server>`.
- A deterministic station may also call tools directly through
  `_execute_tool(name, args)` (archify's `_run_cli` pattern) — same
  three-layer grants still apply via `_resolve_tools`.

### Tool design rules (quality, not mechanics)

- **One tool, one purpose.** The model picks tools by description, so
  descriptions must disambiguate siblings — say when NOT to use it, too.
- **Constrain the schema** (`enum` / `minimum` / `maxLength` / `required`,
  reject extras): a call rejected at validation fails cheaply and
  self-correctably; the same bad argument reaching the handler costs a
  wasted round.
- **Error text is a prompt.** `tool_error` must name the offending field,
  why it failed, and a valid example — the model's next call is written
  from exactly this string. `"bad input"` buys you the same failure again.
- **Reads never mutate; retries must be safe.** Loop rounds, resumes, and
  re-executions re-run your handler — side effects belong to explicitly
  named write tools.
- **External calls declare a timeout and bounded retries.** An unbounded
  network call inside a station eats the whole step budget.

## Builtin defaults you get for free

`default_loop` / `default_fsm` (`atoms/executors/__init__.py` — registered
by host bootstrap and tests/conftest.py), builtin stages (`atoms/stages/`),
`tool_guard` hooks (`atoms/hooks/tool_guard.py`). An unresolved executor
code fails fast with a pointer to `atoms.executors` — that means your
registration/inside import wiring is wrong, not the registry.
