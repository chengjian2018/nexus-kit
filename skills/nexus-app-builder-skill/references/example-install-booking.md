# Exemplar dissection: `apps/install_booking_agent/` (FSM pipeline)

The reference answer for guided-conversation apps: the assistant drives,
the user answers, **one node advances per turn**. Source:
`apps/install_booking_agent/route.py` (16 nodes + pattern),
`stages.py` (app-local stages), `prompts.py`,
`tests/test_install_booking_agent_route.py` (the route-test idiom).

## Shape: a hand-drawn dialogue sketch → a declarative graph

Origin note: this app was transcribed from a photo of a hand-drawn flow —
sketch oval → node code → labeled edges → `sub_nodes`. That mapping
(greet → confirm address → check arrival → negotiate time → confirm → end)
is exactly the fsm authoring method: **draw the conversation, then declare
it**.

Structural patterns worth stealing:

- **Every business node carries a decline edge** (`install_decline` is the
  generic exit channel — unreachable-by-sketch intents don't derail the
  flow; two beats: empathetic reply node → goodbye node).
- **Terminal ovals merge** into one `is_end` node (`install_end`) when the
  closing behavior is identical ("polite hang-up") — the reply paradigm
  comes from `answer_examples` of the *chosen* next node.
- **Cycles are free**: `install_reschedule → install_ask_time → …` re-enters
  negotiation with no budget (fsm semantics: one advance per turn, always).
- **Slots per node** declare what the turn tries to collect
  (`slots={"arrived": "商品是否已到货（是/否）"}`) — they flow into NLU prompts
  and accumulate into `cxt.filled_slots` on transition.

## The stages skeleton (where fsm behavior lives)

```python
stages=[
    {"query": "time_aug_query"},        # builtin: rewrite "明天下午" → absolute time
    {"nlu": "install_unified"},         # app-local: ONE LLM call → reply + next_node + slots
    {"clarify": "install_clarify"},     # app-local: keyword-gated off-topic answers
    {"nlg": "nlg_pass_through"},        # builtin: keep the unified reply (no 2nd LLM call)
]
```

`install_unified` (`stages.py`) is the workhorse: one prompt carrying node
context, task_info (order facts + installer schedule `available_slots`),
and the legal `next_node` set; it writes reply + transition + slots in one
call. **Deterministic guards ride the stage, not the nodes**:

- Bookable-time guard: an unbookable visit-time pick is deterministically
  rerouted to `install_recommend` with a schedule-backed reply — zero extra
  LLM. This is the "precise adjudication in code" triage rule applied to fsm.
- Clarify admission switch: declaring `stages={"clarify": "install_clarify"}`
  on a node IS the per-node switch for off-topic handling.

⚠️ **The one-turn-late trap** (documented in route.py:75-80): FSM transitions
fire at end of turn, so a **node-level NLG stage resolves on the NEXT turn**
and clobbers that turn's reply. Same-turn deterministic rewrites must ride
the stage that chose the transition (that is why the recommend rewrite lives
inside `install_unified`, not on `install_recommend`'s node-level nlg).

## When you'd choose this over agent

Per-turn human reply is the product (call centers, form filling, intake,
negotiation); the deliverable is the conversation itself. If instead one
message should yield an end-to-end artifact, that's agent (see the archify
dissection). Hybrid needs (occasionally pause for input inside a pipeline)
belong to agent + `wait_human`, not fsm.

## The route-test idiom (copy this for every new app)

`tests/test_install_booking_agent_route.py` — fully offline, no network:

- Fixtures: `register_fake_provider()` (session-scoped, from
  `tests/fake_provider.py`), `discover_builtin_patterns()` (module-scoped —
  **assert your route module is in the imported list**), fresh `sessions`
  dict per test.
- `launch()`: build a `Session`, inject `pattern`, `node_map`, `task_info`,
  `llm_override=fake_llm_config()`.
- `chat()`: run `nexus.engine.chat.chat(query, session_id, all_sessions)`
  via `tests/async_utils.py::arun`.
- Structure test: assert node list, every `sub_nodes` target exists, entry,
  `is_end`, stages skeleton resolves (`plugin_registry.has("stage", ...)`).
- `validate_pattern(pattern)` must pass (app-local codes resolve).
- Behavior walks: multi-turn scripted dialogues asserting
  `session.cxt.current_node_code` per turn, `filled_slots` accumulation,
  `conversation_end` actions on terminal entry, LLM call counts.
- Edge-matrix tests via `@pytest.mark.parametrize` (e.g. five decline
  intents, lateral branches, guard reroutes).

Your new app's `tests/test_<app>_route.py` should follow exactly this shape;
it doubles as the offline smoke test (Phase 4 of SKILL.md).
