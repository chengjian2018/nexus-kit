"""Test helper: register inline stub stages into the plugin registry.

Stages declarations are string codes resolved from the plugin
registry; tests that used to attach inline stub stage objects now register
the stub class under a unique code (kind="stage") and declare that code.
``register_stage_stub`` returns the code. Registrations are process-global
(idempotent per (code, factory) pair) — use unique codes per test shape.
"""

import itertools

from nexus.registry.plugins import registry

_counter = itertools.count()


def register_stage_stub(stage_cls, prefix="stub"):
    """Register a stage class (or any zero-arg factory) under a unique code.

    Returns the code to declare in stages / node.stages.
    """
    code = f"{prefix}_{next(_counter)}"
    registry.register("stage", code, stage_cls)
    return code


def declare(stages_dict):
    """Copy a stages declaration dict (convenience; kept trivial)."""
    return dict(stages_dict or {})
