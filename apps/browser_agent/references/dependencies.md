# Dependency Strategy

This skill ships dependency metadata and bootstrap scripts, but setup is lazy.

## Optional Python Packages

See `scripts/requirements-optional.txt`:

- `cloakbrowser` for the `cloak` engine.
- `playwright` for the `playwright` engine.

browser-act is not a Python package dependency here. Install it lazily as a uv tool only when the `browser-act` engine is selected: `uv tool install browser-act-cli --python 3.12`.

## Lazy Setup Commands

Check only:

```bash
python scripts/bootstrap_browser_engines.py --engines cloak,browser-act,kimi,playwright
```

Install selected engines in the active venv / managed Python environment:

```bash
python scripts/bootstrap_browser_engines.py --engines browser-act --install
python scripts/bootstrap_browser_engines.py --engines cloak,playwright --install
```

## browser-act

No browser-act dependency is installed at skill-load time. When fallback reaches `browser-act`, the orchestrator first checks for `browser-act` or `BROWSER_ACT_BIN`. If missing and the caller supplied `--install-missing`, it runs `uv tool install browser-act-cli --python 3.12`. If `uv` is missing, report `dependency_missing` and ask the caller to install uv or browser-act manually.

## Kimi

No package is installed for Kimi at skill-load time. Configure Kimi only when fallback reaches it. See `references/kimi-webbridge.md`.

## Isolation Rule

When running inside WorkBuddy, prefer the managed Python venv or current project venv. Avoid global `pip install` unless the user explicitly approves.