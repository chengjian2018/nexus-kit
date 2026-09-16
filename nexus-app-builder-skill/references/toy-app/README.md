# toy-app: `note_polish` — the minimal agent app with a review loop

A complete, working nexus-kit app in four nodes:

```
np_draft ──> np_review ──┬─ pass / max_rounds / stale ──> np_deliver (is_end)
 entry,custom    │        │        ↑
  writes memo    │        └──> np_revise (custom) ───────┘
                 │                 reads critique history, writes revised memo
 convergence gate here (deterministic, never model-judged)
```

- **Business**: polish rough meeting notes into a structured memo; a reviewer
  critiques it; a reviser applies the critique — the reviser **inherits every
  previous round's critique and fix history** (the design→verify→fix→verify
  pitfall, in miniature).
- **Pattern**: `pattern_type="agent"`, entry `np_draft`, `max_steps=10`
  (a review+revise round costs 2 steps; the semantic gate stops earlier).
- **No tools**: file I/O is done deterministically by the executors
  (`Path.write_text`), so `allow_toolset` stays empty — deny-by-default in
  its cleanest form. Add tools the same way archify does when you need them.

## This directory is inert by design

It sits under `nexus-app-builder-skill/references/` — outside `apps/`, so
`host/main.py`'s AST discovery will never import it, and the architecture
test ignores it. It becomes live only when copied under `apps/`.

## Install (30 seconds)

```bash
cp -r nexus-app-builder-skill/references/toy-app apps/note_polish_agent
# toy-app has a hyphen (not importable); the destination uses underscores.
# Files are already named for apps/note_polish_agent/ — no renames needed.
```

Then verify (Phase 4 of the skill):

```bash
pytest tests/test_architecture.py          # layering still green
pytest tests/test_note_polish_agent_route.py   # after you add the test below
```

## What each file teaches

| File | Lesson |
|---|---|
| `route.py` | Node declaration, one-to-one `plugins={"loop": "<node code>"}` binding, `registry.register(pattern)` + bottom executor import |
| `executor.py` | The state-board idiom (`_new_state`/`_load_state`/`_save_state`), LLM call idiom (`build_provider` + `build_agent_messages` + `_stream_round`), JSON protocol with one retry, deterministic convergence gate, **experience inheritance** (`critique_log` + `revision_log`), honest exit, force-close handling |
| `prompts.py` | Prompt assets live in their own module; phase prompts are templates over state |
| `config.yaml` | `pattern:` binding, `llm:` defaults, `loop:` budgets, free `config:` bag read via `get_pattern_custom_config` |
| `__init__.py` | Empty — but required (apps are packages) |

Map to the real exemplars: this is `apps/archify_agent/` with the geometry,
tools, and stations you don't need yet stripped out. When your app grows
(gates that run real validators, fan-out, delivery), graduate to forking
archify / deep_research instead.

## Route test template

Create `tests/test_note_polish_agent_route.py` (offline, fake provider —
idiom from `tests/test_install_booking_agent_route.py`):

```python
import pytest
from fake_provider import register_fake_provider, fake_llm_config

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    register_fake_provider()

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry
    imported = discover_builtin_patterns()
    assert "apps.note_polish_agent.route" in imported, imported
    return registry.get("note_polish")

def test_structure(pattern):
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "np_draft"
    assert pattern.node_map["np_deliver"].is_end is True
    for node in pattern.nodes:               # no dangling edges
        for target in node.sub_nodes:
            assert target in pattern.node_map
    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)                # plugin codes resolve

def test_offline_smoke_round(pattern, tmp_path, monkeypatch):
    """One full graph run with the scripted fake provider: registration,
    dispatch, state board, and routing all wired end to end."""
    from apps.note_polish_agent import executor as ex
    monkeypatch.setattr(ex, "_DEFAULT_WORKSPACE_ROOT", str(tmp_path))
    # TODO: script FakeProvider outputs per call (draft memo, then review
    # verdicts) — see fake_provider.py's scripting mechanism — then run
    # launch() + chat() like test_install_booking_agent_route.py and assert
    # the final reply carries the memo and the round summary.
```

Structure + validation are enforced from day one; the scripted smoke walk is
the part you finish per your FakeProvider scripting needs.

## Where the pitfalls live in this code

- Loop inheritance: `NpReviseExecutor` builds its prompt from
  `critique_log` + `revision_log` — without them round 3 would repeat
  round 1's fix (see `references/pitfalls.md` #1).
- Honest exit: the stale rule (`done_reason="stale"`) mirrors archify's
  `_trailing_stale` — stop polishing, report honestly.
- Reply semantics: only `np_deliver` returns non-empty `content`; every
  other station is silent (`references/pitfalls.md` #6).
- Placement is code's: the model returns memo text; the executor writes the
  file at the absolute state-board path (`references/pitfalls.md` #10).
