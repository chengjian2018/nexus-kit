"""Pytest tests-dir config: put tests/ itself on sys.path (the shared
fake_provider helper is imported bare by test modules, including the
tests/clarify/ subpackage) and warm up the atom defaults."""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import atoms.stages  # noqa: F401,E402 -- registers kernel default-stage factories
