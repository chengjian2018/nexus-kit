# Pattern & BaseNode schema

Curated from `nexus/model/pattern.py`, `nexus/model/node.py`,
`nexus/model/serialization.py`. Verify against those files (or
`python nexus-introspect-skill/introspect.py pattern <code> --view yaml`)
before writing code — this copy can drift.

## Pattern (the data template / graph)

```python
Pattern(
    code="my_app",                  # unique registry key (lowercase snake)
    name="展示名",
    description="What this pattern does (feeds prompts)",
    pattern_type="agent",           # "fsm" | "agent" (default "agent")
    entry_node_code="my_entry",     # default: nodes[0].code
    nodes=[...],                    # BaseNode objects (or dicts); empty → one auto node
    stages=[{"query": "..."}, ...], # FSM only; AGENT declaring stages raises
    plugins={"loop": "...", "fsm": "...", "messages_builder": "..."},  # pattern-level executor defaults
    allow_toolset=["shell"],        # toolset grants; empty = no toolsets at all
    allow_skills=["archify"],       # skill-asset grants; empty = none
    config={"max_steps": 10, "max_fanout": 8, ...free bag...},
    **kwargs,                       # folds into config (explicit params win)
)
registry.register(pattern)          # module level in route.py — that's the discovery trigger
```

Compile-time validation (constructor raises, fail fast):

- `pattern_type` must be `"fsm"`/`"agent"`; anything else → `ValueError`.
- Dangling edges: any `node.sub_nodes` target missing from `nodes` → raises.
- Duplicate node codes → raises; empty node code → raises.
- AGENT pattern with any node declaring `slots` → raises (business state
  goes in `config` free fields for agent nodes).
- AGENT pattern declaring `stages` → raises (stages pipeline is FSM-only).
- `entry_node_code` not in nodes → raises.

Budgets pinned into config: `max_steps` (AGENT step budget, default 10 —
one node execution per step; FSM never consumes it), `max_fanout`
(per-`sends` worker-instance cap, default 8).

## BaseNode (one node)

```python
BaseNode(
    code="my_stage",                # unique within the pattern
    name="节点名",
    description="scenario description (NLG cur_node facet)",
    task_description="what this node is trying to do (NLU cur_node facet)",
    sub_nodes=["next_a", "next_b"], # FSM: legal next_node set; AGENT: adjacency.
                                    # Conditional edges are runtime TurnResult.next,
                                    # always mapped back onto these declared codes.
    answer_examples=["..."],        # reply-paradigm examples (prompt asset)
    stages={"clarify": "..."},      # FSM only: per-slot stage override
    slots={"slot_name": "说明"},    # FSM only; AGENT node declaring slots raises
    use_tools=["read_text"],        # EMPTY = no tools (deny-by-default)
    use_skills=["archify"],         # EMPTY = no skills
    is_end=False,                   # terminal marker
    plugins={"loop": "my_exec"},    # node-level executor binding (overrides pattern)
    config={...},                   # free bag; prompt assets ride here via **kwargs:
    base_prompt="...",              #   base_prompt / base_nlu_prompt / base_nlg_prompt
    base_nlu_prompt="...",          #   read at runtime via node.get_prompt(key)
)
```

Field semantics that matter at design time:

- `use_tools` is the **node-level allowlist**; effective tools =
  toolset members of `pattern.allow_toolset` ∩ `node.use_tools`
  (`nexus/engine/loop.py::_resolve_tools`). An empty `use_tools` node has
  ZERO tools even when the pattern grants toolsets — deliberate.
- `plugins={"loop": "<executor code>"}` binds an AGENT node to a custom
  executor. Omit it to run the node on `default_loop` (ReAct-style LLM +
  tools loop driven by `base_prompt`) — that is the cheapest way to build
  a node.
- Prompt assets (`base_prompt` etc.) land in `node.config`; they are kwargs
  sugar, not named constructor params.
- `sub_nodes` is the ONLY place edges exist. There are no edge objects.
  Every legal routing target must be declared, including "escape" edges
  (e.g. a failure shortcut to the report node) — an undeclared target
  terminates the graph with a warning.

## YAML round-trip

The same shape serializes to YAML (`pattern_to_dict/from_yaml`,
`nexus/model/serialization.py`) — that is what the studio works with. The
Python declaration in `route.py` is the canonical path for code apps; you
will normally never hand-write the YAML. To see any registered pattern in
YAML: `python nexus-introspect-skill/introspect.py pattern <code> --view yaml`.

## Registration & discovery (the idiom that makes it an app)

`apps/<name>/route.py`:

```python
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

my_node = BaseNode(code="my_entry", ...)
my_pattern = Pattern(code="my_app", ..., nodes=[my_node, ...])
registry.register(my_pattern)

# bottom import closes the plugin binding loop (executor/stages self-register
# at module level there) — same convention as archify / install_booking:
import apps.<name>.executor  # noqa: E402,F401
```

`host/main.py` (and tests) call `discover_builtin_patterns()`, which
AST-scans `apps/*/*.py` for top-level `registry.register(...)` calls and
imports the files. Consequences:

- Registration code must sit at module top level (not inside functions/classes).
- Plugin codes are GLOBAL: a forked app that keeps the template's executor
  codes raises `插件冲突` (same code, different factory). Rename codes when
  forking (node codes are pattern-local and only need internal uniqueness).
- `apps/<name>/config.yaml` top key `pattern: <code>` binds the app dir to
  the pattern (dir name ≠ pattern code, e.g. `apps/archify_agent/` → `archify`).
