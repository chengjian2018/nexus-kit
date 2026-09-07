"""Pytest root configuration — puts the repo root on sys.path so the flat
top-level packages (nexus/atoms/apps/host) are importable without installing,
and warms up the atom defaults (stage fallbacks registered by atoms.stages)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import atoms.stages  # noqa: F401,E402 -- registers kernel default-stage factories
