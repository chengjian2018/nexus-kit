# Browser Engine Contract

## Engine Result

Each engine returns a JSON-serializable result:

```json
{
  "ok": true,
  "engine": "cloak",
  "attempt": 1,
  "url": "https://example.com",
  "current_url": "https://example.com",
  "outputs": {"title": "Example Domain"},
  "artifacts": ["screenshot.png"],
  "error": null,
  "failure_kind": null
}
```

On failure:

```json
{
  "ok": false,
  "engine": "cloak",
  "attempt": 2,
  "error": "Timeout 30000ms exceeded",
  "failure_kind": "network_or_timeout",
  "artifacts": ["cloak_attempt2_error.png"]
}
```

## Failure Kinds

Use stable labels so callers can decide whether to retry or switch engines:

- `dependency_missing`: package, browser binary, bridge, or extension missing.
- `setup_required`: user action is required, e.g. Kimi extension or login is not configured.
- `login_required`: page blocks automation until user logs in.
- `captcha_required`: captcha or safety challenge appears.
- `selector_drift`: target selector is missing or changed.
- `network_or_timeout`: navigation timeout, DNS, request hang, or rate limit.
- `engine_capability_gap`: engine cannot support the required action.
- `unknown`: fallback when classification is not reliable.

## Priority Rules

Default: `cloak,browser-act,kimi,playwright`.

Override only when the task has a strong reason:

- Local dev webapp / deterministic UI test: `playwright,cloak,browser-act,kimi` is acceptable.
- High-risk external website / anti-bot prone page: keep `cloak` first, then `browser-act`.
- Agent-oriented session/proxy/human-collaboration workflow: `browser-act,cloak,kimi,playwright` is acceptable if user explicitly requests browser-act.
- Kimi-specific browser plugin workflow: `kimi,cloak,browser-act,playwright` is acceptable if user explicitly requests Kimi.

## Adding a New Engine

When adding an engine, update both `SKILL.md` and `scripts/browser_orchestrator.py`:

1. Add an engine class with `name`, `check()`, and `run_once()`.
2. Make dependency setup lazy; only check/install when the engine is selected.
3. Map errors to `failure_kind`. If the engine depends on an external CLI such as browser-act, document the adapter command contract instead of guessing subcommands.
4. Add the engine to `ENGINE_REGISTRY`.
5. Declare its priority position in the calling skill or plan.
6. Run `check` and at least one smoke test.

Do not silently insert an engine into the default order without explaining why.